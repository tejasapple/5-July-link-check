import asyncio
import logging
import os
import re
import time
import html
import random
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
BOT_TOKEN = "8592502828:AAEJgC0dh-dYMtx8CqO_Qomiz53HRR5HTSs"
API_ID    = 32003552
API_HASH  = "18e677db0dc3bb8cf89c574a6f460cc3"

ADMIN_ID  = 8884734704

# बेसिक चैनल्स
ACTIVE_CHANNEL_ID  = -1004458234660
EXPIRED_CHANNEL_ID = -1003934489318
FORWARD_ON_CHANNEL_ID = -1004340697685
CHATTING_ON_CHANNEL_ID = -1003789944143
SKIPPED_CHANNEL_ID = -1003934489318

# 1. मेंबर्स के अकॉर्डिंग चैनल्स (सिर्फ वही जिनमें चैटिंग ऑन है)
MEMBERS_LESS_1000_ID = -1000000000001
MEMBERS_1000_2500_ID = -1000000000002
MEMBERS_2500_5000_ID = -1000000000003
MEMBERS_5000_PLUS_ID = -1000000000004

# 2. ऐड मेंबर + चैटिंग/मीडिया चैनल्स
ADD_MEMBER_TEXT_CHAT_ID = -1004496472899   # चैटिंग (Text) ऑन + ऐड मेंबर ऑन
ADD_MEMBER_MEDIA_CHAT_ID = -1000000000005  # सिर्फ मीडिया ऑन (Text ऑफ) + ऐड मेंबर ऑन

SESSIONS_DIR  = "sessions"
USERS_FILE = "users.txt"
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
DUPLICATE_CACHE = {}  

CHECKER_STATE = {}

def clean_html_text(text: str) -> str:
    if not text: return "Unknown"
    return html.escape(str(text))

def get_user_sessions(uid: int) -> list:
    sessions = []
    prefix = f"u{uid}_"
    try:
        for file in os.listdir(SESSIONS_DIR):
            if file.startswith(prefix) and file.endswith(".session"):
                base_name = file.replace(".session", "")
                path = os.path.join(SESSIONS_DIR, base_name)
                if path not in sessions:
                    sessions.append(path)
    except: pass
    return sorted(sessions, key=lambda x: int(x.split('_')[-1]) if '_' in x else 0)

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
    link = link.strip().rstrip("-.,_ \n\t*`~")
    for _ in range(2): 
        try:
            async with aiohttp.ClientSession() as s:
                headers = {"User-Agent": "Mozilla/5.0"}
                async with s.get(link, timeout=5, headers=headers) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        if any(x in text for x in ["Invite link is invalid", "Link is invalid", "has expired", "tgme_page_error", "not found"]):
                            return "expired"
                        if "If you have Telegram, you can contact" in text and "@" in text:
                            if "Join Channel" not in text and "Send Message" not in text and "View in Telegram" not in text:
                                return "expired"
                        if any(x in text for x in ["Join Group", "Join Channel", "View in Telegram", "View Channel"]):
                            return "active"
                        return "unknown"
                    elif resp.status == 404:
                        return "expired"
        except:
            await asyncio.sleep(0.5)
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
            # Expired Username False Positives Fix
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
                    await asyncio.sleep(3) 
                    try: chat = await app.get_chat(chat.id)
                    except: pass
                except UserAlreadyParticipant:
                    chat = await app.get_chat(link)
                except Exception as inner_e:
                    err_msg = str(inner_e).lower()
                    if "invite_request_sent" in err_msg:
                        await asyncio.sleep(3)
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
            
            # Permissions Check (Text vs Media & Add Member)
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

            # Private Channel Media Count Fix
            if joined_now:
                await asyncio.sleep(2) 
                
            for _ in range(2): 
                try: 
                    result["videos"] = str(await app.search_messages_count(chat.id, filter=enums.MessagesFilter.VIDEO))
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                except: 
                    await asyncio.sleep(1)
                    
            for _ in range(2):
                try: 
                    result["photos"] = str(await app.search_messages_count(chat.id, filter=enums.MessagesFilter.PHOTO))
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value)
                except: 
                    await asyncio.sleep(1)

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
            result["title"] = "Uncheckable / Error"
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
            
            # Member Based Routing (Only if Chat is ON)
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
    elif queue_left == 0: eta_str = "Done"
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
        f"📊 <b>Processed:</b> <code>{stats['processed']}</code> | <b>In Queue:</b> <code>{queue_left}</code>\n"
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
#  BULK RUNNER WITH QUEUE
# ─────────────────────────────────────────
async def _run_bulk_check(uid: int, cid: int, sessions: list):
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

    while USER_QUEUES.get(uid):
        try:
            if QUEUE_CONTROL.get(uid) == "stopped": break
            if QUEUE_CONTROL.get(uid) == "paused":
                await _update_dashboard_if_needed(uid)
                await asyncio.sleep(1)
                continue

            item = USER_QUEUES[uid].pop(0)
            lnk = item["link"]
            bunch_msg_id = item["message_id"]

            if bunch_msg_id and bunch_msg_id != current_pinned_msg_id:
                await _pin_message(cid, bunch_msg_id)
                current_pinned_msg_id = bunch_msg_id

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

    if uid in DUPLICATE_CACHE: del DUPLICATE_CACHE[uid]

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
        [{"text": "🔗 Check Links", "callback_data": "menu_check"}],
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

    elif d == "menu_accounts":
        sessions = get_user_sessions(uid)
        kb = [[{"text": "➕ Login New ID", "callback_data": "login_new"}], [{"text": "🩺 Check Accounts Status", "callback_data": "check_health"}]]
        for s in sessions:
            base_name = os.path.basename(s)
            kb.append([{"text": f"🗑 Logout ID: {base_name}", "callback_data": f"logout_{base_name}"}])
        if len(sessions) > 1: kb.append([{"text": "🗑 Logout All IDs", "callback_data": "logout_all"}])
        kb.append([{"text": "🔙 Back", "callback_data": "back_main"}])
        await _edit_raw(cid, mid, f"📱 <b>Account Manager</b>\n\nLogged in IDs: <b>{len(sessions)}</b>\n\nYou can logout specific IDs, add new ones, or check their health.", kb)

    elif d == "check_health":
        await _edit_raw(cid, mid, "⏳ <b>Checking health of all logged-in IDs...</b>\n\n<i>This might take a moment.</i>")
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
        ctx.user_data["slot"] = get_next_slot(uid)
        await _edit_raw(cid, mid, "📱 Send your Telegram Phone Number with country code.\nExample: <code>+919876543210</code>", [[{"text": "🔙 Cancel", "callback_data": "menu_accounts"}]])

    elif d == "menu_check":
        sessions = get_user_sessions(uid)
        if not sessions:
            await _edit_raw(cid, mid, "❌ <b>No IDs Found!</b>\n\nPlease go to 'Manage IDs' and login at least 1 account before checking links.", [[{"text": "🔙 Back", "callback_data": "back_main"}]])
            return
        ctx.user_data["mode"] = "checking_links"
        await _edit_raw(cid, mid, f"🔗 <b>SEND LINKS NOW</b>\n\nSend up to unlimited links (Forward chunks smoothly).\nBot will process them securely using your {len(sessions)} logged-in IDs with Fallback Support.", [[{"text": "🔙 Back", "callback_data": "back_main"}]])

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id; cid = update.effective_chat.id
    text = (update.message.text or update.message.caption or "").strip()
    mode = ctx.user_data.get("mode", "")

    if text == "/start": return 

    if mode == "setting_delay":
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
            slot = ctx.user_data["slot"]
            app = Client(os.path.join(SESSIONS_DIR, f"u{uid}_{slot}"), api_id=API_ID, api_hash=API_HASH)
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
        if uid not in DUPLICATE_CACHE: DUPLICATE_CACHE[uid] = set()
            
        bunch_msg_id = update.message.message_id
        added_count = duplicate_count = 0

        for l in links:
            if l not in DUPLICATE_CACHE[uid]:
                USER_QUEUES[uid].append({"link": l, "message_id": bunch_msg_id})
                DUPLICATE_CACHE[uid].add(l)
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
        asyncio.create_task(_run_bulk_check(uid, cid, sessions))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    print("Bot is running perfectly with Smart Features...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
