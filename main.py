import asyncio
import logging
import os
import re
import time
import html
import random
import json
import aiohttp

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from pyrogram import Client, enums
from pyrogram.errors import (
    SessionPasswordNeeded, FloodWait, UsernameInvalid, 
    UsernameNotOccupied, ChannelPrivate, UserAlreadyParticipant,
    ChannelBanned, PeerIdInvalid, BadRequest, ChatAdminRequired,
    InviteHashExpired, InviteHashInvalid 
)
from pyrogram.raw.functions.messages import CheckChatInvite
from pyrogram.raw.types import ChatInviteAlready, ChatInvite

# ─────────────────────────────────────────
#  CONFIG 
# ─────────────────────────────────────────
BOT_TOKEN = "8278209344:AAHKR4ejDLyw2ikJs6ATjFgHRLI7Vxl-MKg"
API_ID    = 32003552
API_HASH  = "18e677db0dc3bb8cf89c574a6f460cc3"

ADMIN_ID  = 8884734704

# ⚠️ यहाँ अपना स्टोरेज चैनल/ग्रुप ID डालें जहाँ स्क्रैपर लिंक्स भेजेगा
STORAGE_CHANNEL_ID = -1004448809511   

# बेसिक चैनल्स
ACTIVE_CHANNEL_ID  = -1004458234660
EXPIRED_CHANNEL_ID = -1003934489318
FORWARD_ON_CHANNEL_ID = -1004340697685
CHATTING_ON_CHANNEL_ID = -1003789944143
SKIPPED_CHANNEL_ID = -1003934489318

# 1. मेंबर्स के अकॉर्डिंग चैनल्स (सिर्फ वही जिनमें चैटिंग ऑन है)
MEMBERS_LESS_1000_ID = -1004494600592
MEMBERS_1000_2500_ID = -1003701317207
MEMBERS_2500_5000_ID = -1004320671631
MEMBERS_5000_PLUS_ID = -1004320042078

# 2. ऐड मेंबर + चैटिंग/मीडिया चैनल्स
ADD_MEMBER_TEXT_CHAT_ID = -1004334266609    
ADD_MEMBER_MEDIA_CHAT_ID = -1004334266609  

SESSIONS_DIR  = "sessions"
USERS_FILE = "users.txt"
SCRAPER_STATE_FILE = "scraper_state.json"
STORAGE_STATE_FILE = "storage_state.json"  

os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
#  STATE & LOCKS & QUEUES
# ─────────────────────────────────────────
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
LOGIN_STATE = {} 
CHECKING_LOCKS = {}
USER_QUEUES = {}      
QUEUE_CONTROL = {}    
USER_DELAYS = {}      

SCRAPER_DUPLICATES = {}  
CHECKER_DUPLICATES = {}  

CHECKER_STATE = {}
SCRAPER_TASKS = {}

# ─────────────────────────────────────────
#  JSON STATE LOADERS
# ─────────────────────────────────────────
def load_scraper_state(uid: int) -> dict:
    try:
        if os.path.exists(SCRAPER_STATE_FILE):
            with open(SCRAPER_STATE_FILE, "r") as f:
                data = json.load(f)
                return data.get(str(uid), {"channel": None, "start_msg_id": 1, "progress_msg_id": None})
    except: pass
    return {"channel": None, "start_msg_id": 1, "progress_msg_id": None}

def save_scraper_state(uid: int, state: dict):
    try:
        data = {}
        if os.path.exists(SCRAPER_STATE_FILE):
            with open(SCRAPER_STATE_FILE, "r") as f: data = json.load(f)
        data[str(uid)] = state
        with open(SCRAPER_STATE_FILE, "w") as f: json.dump(data, f)
    except: pass

def load_storage_state(uid: int) -> int:
    try:
        if os.path.exists(STORAGE_STATE_FILE):
            with open(STORAGE_STATE_FILE, "r") as f:
                data = json.load(f)
                return data.get(str(uid), 1)
    except: pass
    return 1

def save_storage_state(uid: int, msg_id: int):
    try:
        data = {}
        if os.path.exists(STORAGE_STATE_FILE):
            with open(STORAGE_STATE_FILE, "r") as f: data = json.load(f)
        data[str(uid)] = msg_id
        with open(STORAGE_STATE_FILE, "w") as f: json.dump(data, f)
    except: pass

def clean_html_text(text: str) -> str:
    if not text: return "Unknown"
    return html.escape(str(text))

def get_user_sessions(uid: int, include_scraper=False) -> list:
    sessions = []
    prefix = f"u{uid}_"
    try:
        for file in os.listdir(SESSIONS_DIR):
            if file.startswith(prefix) and file.endswith(".session"):
                sessions.append(os.path.join(SESSIONS_DIR, file.replace(".session", "")))
    except: pass
    sessions = sorted(sessions, key=lambda x: int(x.split('_')[-1]) if '_' in x else 0)
    
    if include_scraper:
        scraper_path = os.path.join(SESSIONS_DIR, f"scraper_{uid}")
        if os.path.exists(scraper_path + ".session"):
            sessions.append(scraper_path)
    return sessions

def get_next_slot(uid: int) -> int:
    sessions = get_user_sessions(uid)
    if not sessions: return 1
    slots = []
    for s in sessions:
        try: slots.append(int(s.split('_')[-1]))
        except: pass
    return max(slots) + 1 if slots else 1

async def cleanup_login_state(uid: int):
    if uid in LOGIN_STATE:
        try: await LOGIN_STATE[uid]["app"].disconnect()
        except: pass
        del LOGIN_STATE[uid]

def track_user(uid: int):
    try:
        if not os.path.exists(USERS_FILE): open(USERS_FILE, "w").close()
        with open(USERS_FILE, "r") as f: users = set(f.read().splitlines())
        if str(uid) not in users:
            with open(USERS_FILE, "a") as f: f.write(f"{uid}\n")
    except: pass

def extract_links(text: str) -> list[str]:
    raw = re.findall(r"(?:https?://)?t\.me/(?:joinchat/|\+)?[a-zA-Z0-9_\-+]+", text)
    out = []
    seen = set()
    for lnk in raw:
        lnk = lnk.rstrip("-.,_ \n\t*`~")
        if not lnk.startswith("http"): lnk = "https://" + lnk
        if lnk not in seen and "t.me/" in lnk:
            seen.add(lnk)
            out.append(lnk)
    return out

def parse_link(link: str) -> tuple[bool, str]:
    link = link.strip().rstrip("-.,_ \n\t*`~")
    m = re.search(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_\-]+)", link)
    if m: return True, m.group(1).rstrip("-")
    m = re.search(r"t\.me/([a-zA-Z0-9_]+)", link)
    if m: return False, m.group(1)
    return False, link

async def fast_http_link_check(link: str) -> str:
    """Super fast check to instantly flag dead links without delaying"""
    link = link.strip().rstrip("-.,_ \n\t*`~")
    for _ in range(1): # Reduced retries for speed
        try:
            async with aiohttp.ClientSession() as s:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                async with s.get(link, timeout=3, headers=headers) as resp: # Strict fast timeout
                    if resp.status == 200:
                        text = await resp.text()
                        if any(x in text for x in ["Invite link is invalid", "Link is invalid", "has expired"]):
                            return "expired" 
                        if "If you have Telegram, you can contact" in text and "@" in text:
                            if "Join Channel" not in text and "Send Message" not in text and "View in Telegram" not in text:
                                return "unknown"
                        if any(x in text for x in ["Join Group", "Join Channel", "View in Telegram", "View Channel"]):
                            return "active"
                        return "unknown"
                    elif resp.status == 404:
                        return "unknown" 
                    elif resp.status == 429:
                        await asyncio.sleep(0.5)
        except:
            await asyncio.sleep(0.1)
    return "unknown"

async def try_check_link(app: Client, link: str):
    is_private, ref = parse_link(link)
    result = {
        "link": link, "status": "skipped", "title": "Unknown", "username": "N/A",
        "members": "N/A", "videos": "N/A", "photos": "N/A", "forward": "N/A", 
        "chatting": "❌ Off", "add_member": "❌ Off", "media_only": False
    }
    
    if not is_private: result["username"] = f"@{ref}"
    chat = None
    joined_now = False

    try:
        if not is_private:
            chat = await app.get_chat(ref)
            if getattr(chat, 'type', None) not in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP, enums.ChatType.CHANNEL]:
                raise UsernameInvalid("Not a group or channel")
        else:
            inv = await app.invoke(CheckChatInvite(hash=ref))
            if isinstance(inv, ChatInviteAlready):
                try: chat = await app.get_chat(inv.chat.id)
                except: chat = await app.get_chat(int(f"-100{inv.chat.id}"))
            elif isinstance(inv, ChatInvite):
                try:
                    chat = await app.join_chat(link)
                    joined_now = True
                    await asyncio.sleep(2) 
                    try: chat = await app.get_chat(chat.id)
                    except: pass
                except UserAlreadyParticipant:
                    chat = await app.get_chat(link)
                except Exception as inner_e:
                    err_msg = str(inner_e).lower()
                    if "invite_request_sent" in err_msg:
                        await asyncio.sleep(1)
                        try:
                            chat = await app.get_chat(link)
                            joined_now = True
                        except Exception:
                            raise inner_e 
                    else:
                        raise inner_e

        result["status"] = "active"
        
        if chat:
            raw_title = getattr(chat, 'title', None) or getattr(chat, 'first_name', "Unknown")
            result["title"] = clean_html_text(raw_title)
            result["members"] = str(getattr(chat, 'members_count', 'N/A'))
            
            has_protected = getattr(chat, 'has_protected_content', False)
            result["forward"] = "❌ Off" if has_protected else "✅ On"
            
            if getattr(chat, 'type', None) in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                if chat.permissions:
                    can_txt = chat.permissions.can_send_messages
                    can_med = chat.permissions.can_send_media_messages
                    can_inv = chat.permissions.can_invite_users
                    
                    result["chatting"] = "✅ On" if can_txt else "❌ Off"
                    result["add_member"] = "✅ On" if can_inv else "❌ Off"
                    
                    if can_med and not can_txt:
                        result["media_only"] = True
                else:
                    result["chatting"] = "✅ On" 
                    result["add_member"] = "✅ On"
            elif getattr(chat, 'type', None) == enums.ChatType.CHANNEL:
                result["chatting"] = "❌ Off (Channel)"
                result["add_member"] = "❌ Off (Channel)"

            if joined_now:
                await asyncio.sleep(2) 
                
            for _ in range(2): 
                try: 
                    result["videos"] = str(await app.search_messages_count(chat.id, filter=enums.MessagesFilter.VIDEO))
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except: 
                    await asyncio.sleep(0.5)
                    
            for _ in range(2):
                try: 
                    result["photos"] = str(await app.search_messages_count(chat.id, filter=enums.MessagesFilter.PHOTO))
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except: 
                    await asyncio.sleep(0.5)

        if joined_now and chat:
            await asyncio.sleep(1)
            try: await app.leave_chat(chat.id)
            except: pass

        return result, False, 0

    except FloodWait as e:
        wait_time = getattr(e, 'value', 30)
        return None, True, wait_time
    except (ChannelBanned, PeerIdInvalid, ChannelPrivate):
        return None, True, 0
    except (InviteHashExpired, InviteHashInvalid, UsernameInvalid, UsernameNotOccupied):
        result["status"] = "expired"
        result["title"] = "Expired / Invalid"
        return result, False, 0
    except Exception as e:
        err_msg = str(e).lower()
        if "expire" in err_msg or "invalid" in err_msg or "not_occupied" in err_msg or "not a group" in err_msg:
            result["status"] = "expired"
            result["title"] = "Expired / Invalid"
            return result, False, 0
        elif "invite_request_sent" in err_msg:
            result["status"] = "active"
            result["title"] = "Admin Approval Required"
            return result, False, 0
        else:
            result["status"] = "skipped"
            result["title"] = f"Error / Skipped"
            return result, True, 0

# ─────────────────────────────────────────
#  INSTANT SENDER & ROUTING
# ─────────────────────────────────────────
async def _send_raw(chat_id: int, text: str, keyboard=None, retries=3):
    if not chat_id or chat_id == 0: return False
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True, "parse_mode": "HTML"}
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    
    async with aiohttp.ClientSession() as s:
        for attempt in range(retries):
            try:
                async with s.post(f"{TG_API}/sendMessage", json=payload) as resp:
                    if resp.status == 200: return await resp.json()
                    elif resp.status == 429:
                        data = await resp.json()
                        await asyncio.sleep(data.get("parameters", {}).get("retry_after", 3) + 0.5)
                    else: await asyncio.sleep(1)
            except: await asyncio.sleep(1)
    return False

async def _pin_message(chat_id: int, message_id: int):
    payload = {"chat_id": chat_id, "message_id": message_id, "disable_notification": True}
    async with aiohttp.ClientSession() as s: await s.post(f"{TG_API}/pinChatMessage", json=payload)

def format_single_message(r: dict) -> str:
    if r['status'] == 'active':
        msg = f"✅ <b>{r.get('title')}</b>\n"
        if r.get('username') != 'N/A': msg += f"👤 <b>Username:</b> {r.get('username')}\n"
        msg += f"👥 <b>Members:</b> <code>{r.get('members')}</code>\n"
        msg += f"🎬 <b>Videos:</b> <code>{r.get('videos')}</code> | 🖼 <b>Photos:</b> <code>{r.get('photos')}</code>\n"
        msg += f"📤 <b>Forward:</b> {r.get('forward')} | 💬 <b>Chat:</b> {r.get('chatting')}\n"
        msg += f"➕ <b>Add Member:</b> {r.get('add_member')}\n🔗 <b>Link:</b> {r['link']}\n"
        return msg
    elif r['status'] == 'skipped':
        return f"⚠️ <b>{r.get('title')}</b>\n👤 <b>Username:</b> {r.get('username')}\n⚠️ <b>Status:</b> Skipped\n🔗 <b>Link:</b> {r['link']}\n"
    else:
        return f"❌ <b>{r.get('title','Expired')}</b>\n👤 <b>Username:</b> {r.get('username')}\n⚠️ <b>Status:</b> <code>Expired</code>\n🔗 <b>Link:</b> {r['link']}\n"

async def dispatch_result(r: dict, stats_tracker: dict):
    msg = format_single_message(r)
    
    if r["status"] == "active":
        await _send_raw(ACTIVE_CHANNEL_ID, f"<b>✅ ACTIVE LINK</b>\n━━━━━━━━━━\n{msg}")
        
        if "✅" in r.get("forward", ""):
            stats_tracker["fwd"] += 1
            await _send_raw(FORWARD_ON_CHANNEL_ID, f"<b>✅ FORWARD ON LINK</b>\n━━━━━━━━━━\n{msg}")
            
        is_chat_on = "✅" in r.get("chatting", "")
        is_add_on = "✅" in r.get("add_member", "")
        is_media_only = r.get("media_only", False)
        
        if is_chat_on:
            stats_tracker["chat"] += 1
            await _send_raw(CHATTING_ON_CHANNEL_ID, f"<b>💬 CHATTING ON LINK</b>\n━━━━━━━━━━\n{msg}")
            try:
                m_count = int(r.get("members", 0)) if r.get("members") != "N/A" else 0
                if m_count < 1000:
                    await _send_raw(MEMBERS_LESS_1000_ID, f"<b>👥 < 1000 MEMBERS (CHAT ON)</b>\n━━━━━━━━━━\n{msg}")
                elif 1000 <= m_count <= 2500:
                    await _send_raw(MEMBERS_1000_2500_ID, f"<b>👥 1000-2500 MEMBERS (CHAT ON)</b>\n━━━━━━━━━━\n{msg}")
                elif 2500 < m_count <= 5000:
                    await _send_raw(MEMBERS_2500_5000_ID, f"<b>👥 2500-5000 MEMBERS (CHAT ON)</b>\n━━━━━━━━━━\n{msg}")
                elif m_count > 5000:
                    await _send_raw(MEMBERS_5000_PLUS_ID, f"<b>👥 5000+ MEMBERS (CHAT ON)</b>\n━━━━━━━━━━\n{msg}")
            except Exception:
                pass
                
        if is_add_on:
            if is_chat_on:
                stats_tracker["add_chat"] += 1
                await _send_raw(ADD_MEMBER_TEXT_CHAT_ID, f"<b>➕ ADD MEMBER & TEXT CHAT ON</b>\n━━━━━━━━━━\n{msg}")
            elif is_media_only:
                stats_tracker.setdefault("add_media", 0)
                stats_tracker["add_media"] += 1
                await _send_raw(ADD_MEMBER_MEDIA_CHAT_ID, f"<b>➕ ADD MEMBER & MEDIA ONLY ON</b>\n━━━━━━━━━━\n{msg}")
                
    elif r["status"] == "expired":
        await _send_raw(EXPIRED_CHANNEL_ID, f"<b>❌ EXPIRED LINK</b>\n━━━━━━━━━━\n{msg}")
    elif r["status"] == "skipped":
        await _send_raw(SKIPPED_CHANNEL_ID, f"<b>⚠️ SKIPPED LINK</b>\n━━━━━━━━━━\n{msg}")

# ─────────────────────────────────────────
#  SCRAPER LOGIC (SENDS TO STORAGE)
# ─────────────────────────────────────────
async def _run_scraper_task(uid: int, cid: int, state: dict):
    scraper_path = os.path.join(SESSIONS_DIR, f"scraper_{uid}")
    if not os.path.exists(scraper_path + ".session"):
        await _send_raw(cid, "❌ Scraper ID is not logged in!")
        return

    channel = state.get("channel")
    start_msg_id = state.get("start_msg_id", 1)
    
    if not channel:
        await _send_raw(cid, "❌ Please set the Target Channel first.")
        return

    app = Client(scraper_path, api_id=API_ID, api_hash=API_HASH, no_updates=True)
    try:
        await app.connect()
        try: chat = await app.get_chat(channel)
        except Exception as e:
            await app.disconnect()
            await _send_raw(cid, f"❌ Failed to get channel: {e}\n(Make sure Scraper ID is inside the channel or it's public)")
            return
            
        await _send_raw(cid, f"🕷️ <b>Scraper Started!</b>\nTarget: {chat.title}\nStarting from Message ID: <code>{start_msg_id}</code>\n<i>Links will be sent directly to your Storage Bot/Channel.</i>")
        
        prog_resp = await _send_raw(cid, f"🔄 <b>Scraper Progress:</b>\nStarting up...")
        prog_msg_id = prog_resp.get("result", {}).get("message_id") if isinstance(prog_resp, dict) else None
        
        if prog_msg_id:
            await _pin_message(cid, prog_msg_id)
            state["progress_msg_id"] = prog_msg_id
            save_scraper_state(uid, state)

        current_id = start_msg_id
        batch_size = 50
        empty_batches = 0
        total_extracted = 0

        if uid not in SCRAPER_DUPLICATES: SCRAPER_DUPLICATES[uid] = set()

        while SCRAPER_TASKS.get(uid) == "running":
            message_ids = list(range(current_id, current_id + batch_size))
            try:
                messages = await app.get_messages(chat.id, message_ids)
                
                chunk_links = []
                
                for msg in messages:
                    if not msg or msg.empty: continue
                    text = (msg.text or msg.caption or "")
                    links = extract_links(text)
                    
                    for l in links:
                        if l not in SCRAPER_DUPLICATES[uid]:
                            chunk_links.append(l)
                            SCRAPER_DUPLICATES[uid].add(l)
                            total_extracted += 1
                
                if chunk_links:
                    empty_batches = 0
                    for i in range(0, len(chunk_links), 50): 
                        send_chunk = chunk_links[i:i+50]
                        text_to_send = "\n".join(send_chunk)
                        try:
                            await _send_raw(STORAGE_CHANNEL_ID, text_to_send)
                        except Exception as e: pass
                        await asyncio.sleep(1) # Fast delay to avoid limits
                else:
                    empty_batches += 1
                    
                current_id += batch_size
                state["start_msg_id"] = current_id
                save_scraper_state(uid, state)

                if prog_msg_id:
                    status_text = (
                        f"🔄 <b>Scraper Progress:</b>\n"
                        f"🎯 <b>Target:</b> {chat.title}\n"
                        f"📍 <b>Processed up to Msg ID:</b> <code>{current_id}</code>\n"
                        f"📥 <b>Links Extracted:</b> <code>{total_extracted}</code>"
                    )
                    payload = {"chat_id": cid, "message_id": prog_msg_id, "text": status_text, "parse_mode": "HTML"}
                    async with aiohttp.ClientSession() as s: await s.post(f"{TG_API}/editMessageText", json=payload)
                
                if empty_batches >= 100: 
                    await _send_raw(cid, "✅ <b>Scraper Finished!</b>\nReached the end of available messages.")
                    SCRAPER_TASKS[uid] = "stopped"
                    break
                    
                await asyncio.sleep(1) # Reduced from 5s to 1s for fast scraping
                
            except FloodWait as e:
                await asyncio.sleep(e.value + 5)
            except Exception as e:
                logger.error(f"Scraper Error: {e}")
                await asyncio.sleep(2)

        await app.disconnect()

    except Exception as e:
        await _send_raw(cid, f"❌ Scraper crashed: {e}")
        try: await app.disconnect()
        except: pass
    
    SCRAPER_TASKS[uid] = "stopped"

# ─────────────────────────────────────────
#  NON-BLOCKING DASHBOARD UPDATER
# ─────────────────────────────────────────
async def _update_dashboard_if_needed(uid: int, force=False):
    state = CHECKER_STATE.get(uid)
    if not state or not state.get("dash_msg_id"): return
    
    now = time.time()
    if not force and (now - state["last_update"] < 5.0):
        return
        
    state["last_update"] = now
    stats = state["stats"]
    queue_left = len(USER_QUEUES.get(uid, []))
    
    elapsed = now - state["start_time"]
    if stats['processed'] > 0 and queue_left > 0:
        avg_time_per_link = elapsed / stats['processed']
        eta_seconds = int(avg_time_per_link * queue_left)
        m, s = divmod(eta_seconds, 60)
        h, m = divmod(m, 60)
        eta_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    elif queue_left == 0: eta_str = "Fetching/Done"
    else: eta_str = "Calculating..."

    perf_strs = []
    for c_key, c_data in state["clients"].items():
        if not c_data["enabled"]: status = "🔴 (Off)"
        elif c_data["ready_at"] > now:
            fw_left = int(c_data["ready_at"] - now)
            status = f"⏳ FW({fw_left}s)"
        else: status = "🟢"
        perf_strs.append(f"{c_data['name']}: {c_data['checks']} {status}")
    
    perf_text = "\n".join(perf_strs)
    last_res = state.get("last_result", {})
    last_checked_text = f"<i>{last_res.get('title', 'None')}</i>" if last_res else "<i>Starting...</i>"

    dash_text = (
        f"<b>⚡ LIVE QUEUE DASHBOARD ⚡</b>\n"
        f"📊 <b>Processed:</b> <code>{stats['processed']}</code> | <b>In Memory Queue:</b> <code>{queue_left}</code>\n"
        f"⏳ <b>Estimated Time Left:</b> <code>{eta_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Active: <code>{stats['active']}</code>\n"
        f"❌ Expired: <code>{stats['expired']}</code>\n"
        f"⚠️ Skipped: <code>{stats['skipped']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 Forward On: <code>{stats['fwd']}</code> | 💬 Chat On: <code>{stats['chat']}</code>\n"
        f"➕ Add Mem(Txt): <code>{stats['add_chat']}</code> | Add Mem(Media): <code>{stats.get('add_media', 0)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>ID Status & Performance:</b>\n<code>{perf_text}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Just Checked:</b>\n{last_checked_text}\n"
    )

    kb = []
    row = []
    for c_key, c_data in state["clients"].items():
        btn_icon = "🟢" if c_data["enabled"] else "🔴"
        row.append({"text": f"{btn_icon} {c_data['name']}", "callback_data": f"tog_id_{c_key}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)

    if QUEUE_CONTROL.get(uid) == "running":
        kb.append([{"text": "⏸️ Pause", "callback_data": "queue_pause"}, {"text": "🛑 Stop", "callback_data": "queue_stop"}])
    else:
        kb.append([{"text": "▶️ Resume", "callback_data": "queue_resume"}, {"text": "🛑 Stop", "callback_data": "queue_stop"}])

    try:
        payload = {"chat_id": state["cid"], "message_id": state["dash_msg_id"], "text": dash_text, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": kb}}
        async with aiohttp.ClientSession() as s: await s.post(f"{TG_API}/editMessageText", json=payload)
    except: pass

# ─────────────────────────────────────────
#  BULK RUNNER WITH QUEUE (AUTO STORAGE PULL ADDED)
# ─────────────────────────────────────────
async def _run_bulk_check(uid: int, cid: int, sessions: list, auto_storage=False):
    QUEUE_CONTROL[uid] = "running"
    
    clients_dict = {}
    for idx, s_path in enumerate(sessions):
        try:
            app = Client(s_path, api_id=API_ID, api_hash=API_HASH, no_updates=True)
            await app.connect()
            slot = str(s_path.split('_')[-1] if '_' in s_path else idx + 1)
            clients_dict[slot] = {"app": app, "ready_at": 0, "name": f"ID {slot}", "checks": 0, "enabled": True}
        except: pass

    if not clients_dict:
        await _send_raw(cid, "❌ Failed to connect any of your logged-in IDs.")
        CHECKING_LOCKS[uid] = False
        return

    CHECKER_STATE[uid] = {
        "clients": clients_dict,
        "stats": {"active": 0, "expired": 0, "skipped": 0, "processed": 0, "fwd": 0, "chat": 0, "add_chat": 0, "add_media": 0},
        "start_time": time.time(),
        "last_update": 0,
        "dash_msg_id": None,
        "cid": cid,
        "last_result": None
    }

    dash_resp = await _send_raw(cid, "⏳ <b>Starting Live Processing Queue...</b>")
    dash_msg_id = dash_resp.get("result", {}).get("message_id") if isinstance(dash_resp, dict) else None
    CHECKER_STATE[uid]["dash_msg_id"] = dash_msg_id

    current_pinned_msg_id = None  
    client_keys = list(clients_dict.keys())
    client_idx = 0

    if uid not in USER_QUEUES: USER_QUEUES[uid] = []
    if uid not in CHECKER_DUPLICATES: CHECKER_DUPLICATES[uid] = set()

    storage_last_msg_id = load_storage_state(uid) if auto_storage else 1
    empty_storage_batches = 0

    while True:
        try:
            if QUEUE_CONTROL.get(uid) == "stopped": break
            if QUEUE_CONTROL.get(uid) == "paused":
                await _update_dashboard_if_needed(uid)
                await asyncio.sleep(1)
                continue

            # Auto Pull Logic From Storage Channel (Fixed Skipping Bug)
            if not USER_QUEUES.get(uid):
                if not auto_storage: 
                    break 
                else:
                    fetched_msg = None
                    messages_received = 0
                    try:
                        c_app = CHECKER_STATE[uid]["clients"][client_keys[0]]["app"]
                        # Fetch batch of 50 messages to prevent stopping early on empty gaps
                        msg_ids_to_fetch = list(range(storage_last_msg_id, storage_last_msg_id + 50))
                        messages = await c_app.get_messages(STORAGE_CHANNEL_ID, msg_ids_to_fetch)
                        
                        links_found_in_batch = False
                        
                        for msg in messages:
                            if not msg or msg.empty: continue
                            messages_received += 1
                            links = extract_links(msg.text or msg.caption or "")
                            
                            for l in links:
                                if l not in CHECKER_DUPLICATES[uid]:
                                    USER_QUEUES[uid].append({"link": l, "message_id": msg.id, "chat_id": STORAGE_CHANNEL_ID})
                                    CHECKER_DUPLICATES[uid].add(l)
                                    links_found_in_batch = True
                                    if not fetched_msg: 
                                        fetched_msg = msg
                        
                        storage_last_msg_id += 50
                        save_storage_state(uid, storage_last_msg_id)
                        
                        if links_found_in_batch:
                            empty_storage_batches = 0
                            if fetched_msg:
                                current_pinned_msg_id = fetched_msg.id
                                await _pin_message(STORAGE_CHANNEL_ID, fetched_msg.id)
                        else:
                            empty_storage_batches += 1
                            
                    except Exception as e:
                        pass
                    
                    if not USER_QUEUES.get(uid):
                        await _update_dashboard_if_needed(uid, force=True)
                        if empty_storage_batches > 20: # Tolerance of 1000 empty messages gap
                            await _send_raw(cid, "✅ <b>Storage Checking Paused/Finished.</b>\nReached the end of available messages in Storage. Will re-check soon if resumed.")
                            break
                        
                        if messages_received == 0:
                            # Reached end of channel, wait for new messages
                            await asyncio.sleep(5) 
                        else:
                            # Messages were fetched but no links found, instantly continue to next chunk
                            await asyncio.sleep(0.1)
                        continue

            item = USER_QUEUES[uid].pop(0)
            lnk = item["link"]
            msg_id = item.get("message_id")
            chat_id = item.get("chat_id")

            if not auto_storage and msg_id and msg_id != current_pinned_msg_id:
                if chat_id: await _pin_message(chat_id, msg_id)
                else: await _pin_message(cid, msg_id)
                current_pinned_msg_id = msg_id

            fast_checked_expired = False
            http_res = await fast_http_link_check(lnk)
            
            if http_res == "expired":
                final_result = {
                    "link": lnk, "status": "expired", "title": "Expired / Invalid",
                    "username": "N/A", "members": "N/A", "videos": "N/A", "photos": "N/A",
                    "forward": "N/A", "chatting": "❌ Off", "add_member": "❌ Off", "media_only": False
                }
                fast_checked_expired = True
            else:
                current_time = time.time()
                selected_key = None
                
                for _ in range(len(client_keys)):
                    k = client_keys[client_idx % len(client_keys)]
                    client_idx += 1
                    c_data = CHECKER_STATE[uid]["clients"][k]
                    if c_data["enabled"] and c_data["ready_at"] <= current_time:
                        selected_key = k
                        break
                
                if not selected_key:
                    USER_QUEUES[uid].insert(0, item) 
                    await _update_dashboard_if_needed(uid)
                    await asyncio.sleep(1) 
                    continue

                c_data = CHECKER_STATE[uid]["clients"][selected_key]
                res, retry_needed, fw_time = await try_check_link(c_data["app"], lnk)
                
                if fw_time > 0:
                    c_data["ready_at"] = time.time() + fw_time
                    USER_QUEUES[uid].insert(0, item) 
                    await _update_dashboard_if_needed(uid)
                    continue 
                    
                final_result = res if res else {"link": lnk, "status": "skipped", "title": "Unknown Error"}
                if final_result["status"] == "active":
                    c_data["checks"] += 1
            
            stats = CHECKER_STATE[uid]["stats"]
            stats["processed"] += 1
            if final_result["status"] == "active": stats["active"] += 1
            elif final_result["status"] == "expired": stats["expired"] += 1
            else: stats["skipped"] += 1
            
            CHECKER_STATE[uid]["last_result"] = final_result
            asyncio.create_task(dispatch_result(final_result, stats))
            await _update_dashboard_if_needed(uid)

            # 🚀 DELAY LOGIC: Now it purely delays on Active Links. 
            # Expired and Skipped links will bypass this instantly.
            if final_result["status"] == "active" and not fast_checked_expired:
                min_del, max_del = USER_DELAYS.get(uid, (10.0, 15.0))
                delay = random.uniform(min_del, max_del)
                start_delay = time.time()
                
                while time.time() - start_delay < delay:
                    if QUEUE_CONTROL.get(uid) in ["stopped", "paused"]: break
                    await _update_dashboard_if_needed(uid)
                    await asyncio.sleep(1) 

        except Exception as e:
            logger.error(f"Error in queue loop: {e}")
            await asyncio.sleep(1) 

    for c_data in CHECKER_STATE[uid]["clients"].values():
        try: await c_data["app"].disconnect()
        except: pass

    if not auto_storage and uid in CHECKER_DUPLICATES:
        CHECKER_DUPLICATES[uid].clear()

    stats = CHECKER_STATE[uid]["stats"]
    perf_strs = [f"{v['name']}: {v['checks']}" for v in CHECKER_STATE[uid]["clients"].values()]
    perf_text = " | ".join(perf_strs)

    status_title = "🛑 QUEUE STOPPED BY USER" if QUEUE_CONTROL.get(uid) == "stopped" else "✨ QUEUE PROCESSING COMPLETED ✨"
    
    final_msg = (f"<b>{status_title}</b>\n\n"
                 f"📊 Total Checked: {stats['processed']}\n"
                 f"✅ Active: <code>{stats['active']}</code> | ❌ Expired: <code>{stats['expired']}</code> | ⚠️ Skipped: <code>{stats['skipped']}</code>\n"
                 f"📤 Fwd On: <code>{stats['fwd']}</code> | 💬 Chat On: <code>{stats['chat']}</code> | ➕ Add Mem(Txt): <code>{stats['add_chat']}</code> | Add Mem(Media): <code>{stats.get('add_media', 0)}</code>\n\n"
                 f"📱 <b>Final ID Performance:</b>\n<code>{perf_text}</code>")
    
    keyboard = [[{"text": "🔙 Main Menu", "callback_data": "back_main"}]]
    if dash_msg_id:
        try:
            payload = {"chat_id": cid, "message_id": dash_msg_id, "text": final_msg, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": keyboard}}
            async with aiohttp.ClientSession() as s: await s.post(f"{TG_API}/editMessageText", json=payload)
        except: await _send_raw(cid, final_msg, keyboard)
    else:
        await _send_raw(cid, final_msg, keyboard)

    CHECKING_LOCKS[uid] = False

# ─────────────────────────────────────────
#  UI & UTILS
# ─────────────────────────────────────────
async def _edit_raw(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    async with aiohttp.ClientSession() as s: await s.post(f"{TG_API}/editMessageText", json=payload)

def MAIN_KB(uid):
    sessions = get_user_sessions(uid)
    return [
        [{"text": "📥 Auto-Check Storage", "callback_data": "menu_storage"}],
        [{"text": "🔗 Manual Check Links", "callback_data": "menu_check"}],
        [{"text": "🕷️ Scraper Menu", "callback_data": "menu_scraper"}],
        [{"text": f"📱 Manage IDs ({len(sessions)} Active)", "callback_data": "menu_accounts"}],
        [{"text": "⚙️ Settings", "callback_data": "menu_settings"}]
    ]

# ─────────────────────────────────────────
#  HANDLERS
# ─────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    track_user(uid)
    ctx.user_data["mode"] = ""
    await cleanup_login_state(uid)
    text = f"👋 <b>Welcome {update.effective_user.first_name}</b>\n\nAdvanced Link Checker Bot.\nYou can login unlimited IDs to check links securely without bans."
    await _send_raw(update.effective_chat.id, text, MAIN_KB(uid))

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; d = q.data; uid = q.from_user.id; cid = q.message.chat.id; mid = q.message.message_id
    track_user(uid)
    async with aiohttp.ClientSession() as s: await s.post(f"{TG_API}/answerCallbackQuery", json={"callback_query_id": q.id})

    if d.startswith("tog_id_"):
        c_key = d.split("tog_id_")[1]
        if uid in CHECKER_STATE and c_key in CHECKER_STATE[uid]["clients"]:
            current_status = CHECKER_STATE[uid]["clients"][c_key]["enabled"]
            CHECKER_STATE[uid]["clients"][c_key]["enabled"] = not current_status
            await _update_dashboard_if_needed(uid, force=True)

    elif d == "queue_pause":
        QUEUE_CONTROL[uid] = "paused"
        await _update_dashboard_if_needed(uid, force=True)

    elif d == "queue_resume":
        QUEUE_CONTROL[uid] = "running"
        await _update_dashboard_if_needed(uid, force=True)

    elif d == "queue_stop":
        QUEUE_CONTROL[uid] = "stopped"
        SCRAPER_TASKS[uid] = "stopped"

    elif d == "back_main":
        await cleanup_login_state(uid)
        ctx.user_data["mode"] = ""
        await _edit_raw(cid, mid, "👋 <b>Main Menu</b>\n\nChoose an option below:", MAIN_KB(uid))

    elif d == "menu_settings":
        min_d, max_d = USER_DELAYS.get(uid, (10.0, 15.0))
        kb = [[{"text": "⏱️ Set Custom Delay", "callback_data": "set_delay"}], [{"text": "🔙 Back", "callback_data": "back_main"}]]
        await _edit_raw(cid, mid, f"⚙️ <b>Settings Panel</b>\n\n⏱️ <b>Current Delay:</b> {min_d}s to {max_d}s\n\n*(Change the delay carefully to avoid getting your IDs banned)*", kb)

    elif d == "set_delay":
        ctx.user_data["mode"] = "setting_delay"
        await _edit_raw(cid, mid, "⏱️ <b>Send your custom delay in seconds.</b>\n\nExample: `5 10` (for a random delay between 5 to 10 seconds)", [[{"text": "🔙 Cancel", "callback_data": "menu_settings"}]])

    elif d == "menu_scraper":
        state = load_scraper_state(uid)
        scraper_path = os.path.join(SESSIONS_DIR, f"scraper_{uid}.session")
        is_logged_in = "✅ Logged In" if os.path.exists(scraper_path) else "❌ Not Logged In"
        
        kb = [
            [{"text": "➕ Login Scraper ID" if not os.path.exists(scraper_path) else "🗑 Logout Scraper ID", "callback_data": "scraper_login_tog"}],
            [{"text": "🎯 Set Target Channel", "callback_data": "scraper_set_target"}],
            [{"text": "📍 Set Start Link (Msg ID)", "callback_data": "scraper_set_start"}],
            [{"text": "▶️ Start Scraping", "callback_data": "scraper_start"}, {"text": "🛑 Stop", "callback_data": "scraper_stop"}],
            [{"text": "🔄 Refresh / Restart Scraper", "callback_data": "scraper_refresh"}],
            [{"text": "🔙 Back", "callback_data": "back_main"}]
        ]
        
        text = (f"🕷️ <b>Scraper Management</b>\n\n"
                f"👤 <b>Status:</b> {is_logged_in}\n"
                f"🎯 <b>Target Channel:</b> <code>{state['channel'] or 'None'}</code>\n"
                f"📍 <b>Start Message ID:</b> <code>{state['start_msg_id']}</code>\n\n"
                f"<i>(Set channel and start point, then click Start. It will extract links safely and send to queue automatically.)</i>")
        await _edit_raw(cid, mid, text, kb)

    elif d == "scraper_login_tog":
        scraper_path = os.path.join(SESSIONS_DIR, f"scraper_{uid}.session")
        if os.path.exists(scraper_path):
            os.remove(scraper_path)
            await _edit_raw(cid, mid, "✅ Scraper ID Logged Out.", [[{"text": "🔙 Scraper Menu", "callback_data": "menu_scraper"}]])
        else:
            ctx.user_data["mode"] = "login_phone"
            ctx.user_data["login_type"] = "scraper"
            await _edit_raw(cid, mid, "📱 Send your Telegram Phone Number with country code for **SCRAPER ID**.\nExample: <code>+919876543210</code>", [[{"text": "🔙 Cancel", "callback_data": "menu_scraper"}]])

    elif d == "scraper_set_target":
        ctx.user_data["mode"] = "scraper_target"
        await _edit_raw(cid, mid, "🎯 <b>Send Target Channel Username or ID</b>\n\nExample: `-10012345678` or `@mychannel`", [[{"text": "🔙 Cancel", "callback_data": "menu_scraper"}]])

    elif d == "scraper_set_start":
        ctx.user_data["mode"] = "scraper_start_link"
        await _edit_raw(cid, mid, "📍 <b>Send Specific Message Link</b>\n\nExample: `https://t.me/c/12345/600`\n<i>(Bot will start scraping from Message ID 600)</i>", [[{"text": "🔙 Cancel", "callback_data": "menu_scraper"}]])

    elif d == "scraper_start":
        if SCRAPER_TASKS.get(uid) == "running":
            await _edit_raw(cid, mid, "⚠️ Scraper is already running!", [[{"text": "🔙 Scraper Menu", "callback_data": "menu_scraper"}]])
            return
        
        state = load_scraper_state(uid)
        SCRAPER_TASKS[uid] = "running"
        asyncio.create_task(_run_scraper_task(uid, cid, state))
        await _edit_raw(cid, mid, "✅ Scraper background task started! Progress will be pinned shortly.", [[{"text": "🔙 Scraper Menu", "callback_data": "menu_scraper"}]])

    elif d == "scraper_stop":
        SCRAPER_TASKS[uid] = "stopped"
        await _edit_raw(cid, mid, "🛑 Scraper stopped.", [[{"text": "🔙 Scraper Menu", "callback_data": "menu_scraper"}]])

    elif d == "scraper_refresh":
        if SCRAPER_TASKS.get(uid) == "running":
            await _edit_raw(cid, mid, "⚠️ <b>Please Stop the scraper first before refreshing!</b>", [[{"text": "🔙 Scraper Menu", "callback_data": "menu_scraper"}]])
            return
            
        state = load_scraper_state(uid)
        state["start_msg_id"] = 1
        if uid in SCRAPER_DUPLICATES:
            SCRAPER_DUPLICATES[uid].clear()
        save_scraper_state(uid, state)
        
        await _edit_raw(cid, mid, "🔄 <b>Scraper Reset Successfully!</b>\n\nAll duplicate cache cleared. Scraper will now start completely fresh from Message ID 1.", [[{"text": "🔙 Scraper Menu", "callback_data": "menu_scraper"}]])

    elif d == "menu_accounts":
        sessions = get_user_sessions(uid)
        kb = [[{"text": "➕ Login New Checker ID", "callback_data": "login_new"}], [{"text": "🩺 Check Accounts Status", "callback_data": "check_health"}]]
        for s in sessions:
            base_name = os.path.basename(s)
            kb.append([{"text": f"🗑 Logout ID: {base_name}", "callback_data": f"logout_{base_name}"}])
        if len(sessions) > 1: kb.append([{"text": "🗑 Logout All IDs", "callback_data": "logout_all"}])
        kb.append([{"text": "🔙 Back", "callback_data": "back_main"}])
        await _edit_raw(cid, mid, f"📱 <b>Checker Account Manager</b>\n\nLogged in IDs: <b>{len(sessions)}</b>\n\nYou can logout specific IDs, add new ones, or check their health.", kb)

    elif d == "check_health":
        await _edit_raw(cid, mid, "⏳ <b>Checking health of all logged-in Checker IDs...</b>\n\n<i>This might take a moment.</i>")
        sessions = get_user_sessions(uid)
        working_count = dead_count = 0
        for s in sessions:
            try:
                app = Client(s, api_id=API_ID, api_hash=API_HASH, no_updates=True)
                await app.connect()
                if await app.get_me(): working_count += 1
                await app.disconnect()
            except Exception: dead_count += 1
        
        kb = [[{"text": "🔙 Back to Manage IDs", "callback_data": "menu_accounts"}]]
        await _edit_raw(cid, mid, f"🩺 <b>Account Status Report</b>\n\n✅ <b>Working IDs:</b> {working_count}\n❌ <b>Dead/Logged Out:</b> {dead_count}\n\n<i>(If you have dead IDs, please find and logout them manually to save resources)</i>", kb)

    elif d.startswith("logout_u"):
        session_name = d.replace("logout_", "")
        path = os.path.join(SESSIONS_DIR, session_name + ".session")
        try: os.remove(path)
        except: pass
        sessions = get_user_sessions(uid)
        kb = [[{"text": "➕ Login New ID", "callback_data": "login_new"}], [{"text": "🩺 Check Accounts Status", "callback_data": "check_health"}]]
        for s in sessions:
            base_name = os.path.basename(s)
            kb.append([{"text": f"🗑 Logout ID: {base_name}", "callback_data": f"logout_{base_name}"}])
        if len(sessions) > 1: kb.append([{"text": "🗑 Logout All IDs", "callback_data": "logout_all"}])
        kb.append([{"text": "🔙 Back", "callback_data": "back_main"}])
        await _edit_raw(cid, mid, f"✅ ID <code>{session_name}</code> Logged Out.\n\n📱 <b>Account Manager</b>\nLogged in IDs: <b>{len(sessions)}</b>", kb)

    elif d == "logout_all":
        for s in get_user_sessions(uid):
            try: os.remove(s + ".session")
            except: pass
        await _edit_raw(cid, mid, "✅ All IDs Logged Out.", [[{"text": "🔙 Back", "callback_data": "menu_accounts"}]])

    elif d == "login_new":
        ctx.user_data["mode"] = "login_phone"
        ctx.user_data["login_type"] = "checker"
        ctx.user_data["slot"] = get_next_slot(uid)
        await _edit_raw(cid, mid, "📱 Send your Telegram Phone Number with country code.\nExample: <code>+919876543210</code>", [[{"text": "🔙 Cancel", "callback_data": "menu_accounts"}]])

    elif d == "menu_check":
        sessions = get_user_sessions(uid)
        if not sessions:
            await _edit_raw(cid, mid, "❌ <b>No IDs Found!</b>\n\nPlease go to 'Manage IDs' and login at least 1 account before checking links.", [[{"text": "🔙 Back", "callback_data": "back_main"}]])
            return
        ctx.user_data["mode"] = "checking_links"
        
        if uid in CHECKER_DUPLICATES:
            CHECKER_DUPLICATES[uid].clear()
            
        await _edit_raw(cid, mid, f"🔗 <b>SEND LINKS NOW</b>\n\nSend up to unlimited links (Forward chunks smoothly).\nBot will process them securely using your {len(sessions)} logged-in IDs with Fallback Support.", [[{"text": "🔙 Back", "callback_data": "back_main"}]])

    elif d == "menu_storage":
        current_id = load_storage_state(uid)
        kb = [
            [{"text": "▶️ Start Auto-Check", "callback_data": "start_storage_check"}],
            [{"text": "🔄 Reset Storage Progress", "callback_data": "reset_storage_check"}],
            [{"text": "🔙 Back", "callback_data": "back_main"}]
        ]
        await _edit_raw(cid, mid, f"📥 <b>Storage Auto-Check Menu</b>\n\n📌 <b>Current Position:</b> Message ID <code>{current_id}</code>\n\n<i>Start checking links automatically from your Storage Channel.</i>", kb)

    elif d == "reset_storage_check":
        save_storage_state(uid, 1)
        if uid in CHECKER_DUPLICATES:
            CHECKER_DUPLICATES[uid].clear()
        kb = [[{"text": "🔙 Back to Storage Menu", "callback_data": "menu_storage"}]]
        await _edit_raw(cid, mid, "✅ <b>Storage Progress Reset!</b>\n\nThe bot will now start checking from the very first message in the Storage Channel. Duplicate cache is also cleared.", kb)

    elif d == "start_storage_check":
        sessions = get_user_sessions(uid)
        if not sessions:
            await _edit_raw(cid, mid, "❌ <b>No IDs Found!</b>\n\nPlease go to 'Manage IDs' and login at least 1 account.", [[{"text": "🔙 Back", "callback_data": "back_main"}]])
            return
        
        ctx.user_data["mode"] = "storage_checking"
        if CHECKING_LOCKS.get(uid):
            await _edit_raw(cid, mid, "⚠️ <b>Queue is already running!</b>", [[{"text": "🔙 Back", "callback_data": "back_main"}]])
            return
            
        CHECKING_LOCKS[uid] = True
        asyncio.create_task(_run_bulk_check(uid, cid, sessions, auto_storage=True))

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id; cid = update.effective_chat.id
    text = (update.message.text or update.message.caption or "").strip()
    mode = ctx.user_data.get("mode", "")

    if text == "/start": return 

    if mode == "scraper_target":
        state = load_scraper_state(uid)
        try:
            val = int(text)
            state["channel"] = val
        except:
            state["channel"] = text
        save_scraper_state(uid, state)
        ctx.user_data["mode"] = ""
        await update.message.reply_text(f"✅ Target Channel set to: <code>{state['channel']}</code>", parse_mode="HTML")

    elif mode == "scraper_start_link":
        m = re.search(r"/(\d+)$", text)
        if m:
            state = load_scraper_state(uid)
            state["start_msg_id"] = int(m.group(1))
            save_scraper_state(uid, state)
            ctx.user_data["mode"] = ""
            await update.message.reply_text(f"✅ Start Message ID set to: <code>{state['start_msg_id']}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Invalid link format. Make sure it ends with a message ID (e.g. `.../600`)", parse_mode="Markdown")

    elif mode == "setting_delay":
        try:
            parts = text.split()
            if len(parts) == 2:
                min_d, max_d = float(parts[0]), float(parts[1])
                if min_d >= 0 and max_d >= min_d:
                    USER_DELAYS[uid] = (min_d, max_d)
                    await update.message.reply_text(f"✅ <b>Delay Updated successfully!</b>\nNew Delay: {min_d}s - {max_d}s", parse_mode="HTML")
                    ctx.user_data["mode"] = ""
                    return
            await update.message.reply_text("❌ <b>Invalid Input.</b>\nEnsure you send two numbers separated by space.", parse_mode="Markdown")
        except: await update.message.reply_text("❌ <b>Invalid Format.</b>", parse_mode="Markdown")

    elif mode == "login_phone":
        if not text.startswith("+") or len(text) < 10:
            await update.message.reply_text("❌ Invalid format. Use +CountryCode Number")
            return
        msg = await update.message.reply_text("⏳ Sending OTP...")
        try:
            ltype = ctx.user_data.get("login_type", "checker")
            if ltype == "scraper":
                s_name = f"scraper_{uid}"
            else:
                s_name = f"u{uid}_{ctx.user_data['slot']}"
                
            app = Client(os.path.join(SESSIONS_DIR, s_name), api_id=API_ID, api_hash=API_HASH)
            await app.connect()
            sent = await app.send_code(text)
            LOGIN_STATE[uid] = {"app": app, "phone": text, "hash": sent.phone_code_hash}
            ctx.user_data["mode"] = "login_otp"
            await msg.edit_text("📩 OTP Sent! Please send the OTP here.\n*(e.g., send `12345` or space-separated `1 2 3 4 5`)*", parse_mode="Markdown")
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}"); ctx.user_data["mode"] = ""

    elif mode == "login_otp":
        otp = text.replace(" ", "")
        if uid not in LOGIN_STATE: return
        data = LOGIN_STATE[uid]; app = data["app"]
        msg = await update.message.reply_text("⏳ Verifying OTP...")
        try:
            await app.sign_in(data["phone"], data["hash"], otp)
            await app.disconnect()
            del LOGIN_STATE[uid]; ctx.user_data["mode"] = ""
            await msg.edit_text("✅ Login Successful!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Menu', callback_data='back_main')]]))
        except SessionPasswordNeeded:
            ctx.user_data["mode"] = "login_pwd"
            await msg.edit_text("🔐 Two-Step Verification is ON. Send your Password:")
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}"); await app.disconnect(); ctx.user_data["mode"] = ""

    elif mode == "login_pwd":
        if uid not in LOGIN_STATE: return
        app = LOGIN_STATE[uid]["app"]
        msg = await update.message.reply_text("⏳ Verifying Password...")
        try:
            await app.check_password(text)
            await app.disconnect()
            del LOGIN_STATE[uid]; ctx.user_data["mode"] = ""
            await msg.edit_text("✅ Login Successful!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🔙 Menu', callback_data='back_main')]]))
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}"); await app.disconnect(); ctx.user_data["mode"] = ""

    elif mode == "checking_links":
        links = extract_links(text)
        if not links: return
            
        sessions = get_user_sessions(uid)
        if not sessions:
            await update.message.reply_text("❌ Please login first.")
            return

        if uid not in USER_QUEUES: USER_QUEUES[uid] = []
        if uid not in CHECKER_DUPLICATES: CHECKER_DUPLICATES[uid] = set()
            
        bunch_msg_id = update.message.message_id
        added_count = duplicate_count = 0

        for l in links:
            if l not in CHECKER_DUPLICATES[uid]:
                USER_QUEUES[uid].append({"link": l, "message_id": bunch_msg_id, "chat_id": cid})
                CHECKER_DUPLICATES[uid].add(l)
                added_count += 1
            else: duplicate_count += 1

        if added_count == 0 and duplicate_count > 0:
            msg = await update.message.reply_text(f"⚠️ <b>Skipped!</b> All {duplicate_count} links were duplicates.", parse_mode="HTML")
            await asyncio.sleep(3)
            try: await msg.delete()
            except: pass
            return

        if CHECKING_LOCKS.get(uid):
            msg_text = f"✅ Added {added_count} new links to Queue."
            if duplicate_count > 0: msg_text += f"\n🗑 Skipped {duplicate_count} duplicate links."
            msg_text += f"\nTotal in Queue: {len(USER_QUEUES[uid])}"
            msg = await update.message.reply_text(msg_text)
            await asyncio.sleep(3)
            try: await msg.delete()
            except: pass
            return

        CHECKING_LOCKS[uid] = True
        asyncio.create_task(_run_bulk_check(uid, cid, sessions, auto_storage=False))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    print("Bot is running perfectly with Smart Features & Fast Storage Pull...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
