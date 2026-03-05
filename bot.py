import os
import re
import json
import time
import sqlite3
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, ContextTypes, CommandHandler, MessageHandler, filters

# ==================== CONFIG ====================
TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOTFATHER_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

# Google Drive (Service Account JSON string)
GOOGLE_DRIVE_JSON = os.getenv("GOOGLE_DRIVE_JSON", "").strip()

# Optional: fixed Drive file id in env (highest priority)
ENV_GDRIVE_FILE_ID = os.getenv("GDRIVE_FILE_ID", "").strip()

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("support_bot")

# ==================== SQLITE ====================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,            -- 'global' | 'user'
            user_id INTEGER,                -- used when scope='user'
            match_type TEXT NOT NULL,       -- 'exact' | 'contains' | 'regex'
            trigger TEXT NOT NULL,          -- normalized for exact/contains, raw for regex
            reply_kind TEXT NOT NULL,       -- 'text' | 'copy'
            reply_text TEXT,                -- if reply_kind='text'
            reply_chat_id INTEGER,          -- if reply_kind='copy'
            reply_message_id INTEGER,       -- if reply_kind='copy'
            created_at INTEGER NOT NULL
        );
        """)
        c.execute("""
        CREATE INDEX IF NOT EXISTS idx_rules_lookup
        ON rules(scope, user_id, match_type, trigger);
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS links (
            admin_message_id INTEGER PRIMARY KEY,
            user_chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS admin_state (
            admin_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """)
        c.commit()

def normalize(s: str) -> str:
    return " ".join((s or "").strip().lower().split())

# ==================== SETTINGS ====================
def set_setting(key: str, value: str):
    with db() as c:
        c.execute("""
        INSERT INTO settings(key, value, updated_at)
        VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET
          value=excluded.value,
          updated_at=excluded.updated_at
        """, (key, value.strip(), int(time.time())))
        c.commit()

def get_setting(key: str) -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

def effective_drive_file_id() -> str:
    if ENV_GDRIVE_FILE_ID:
        return ENV_GDRIVE_FILE_ID
    return get_setting("drive_file_id").strip()

# ==================== ADMIN UI (BUTTON MENU) ====================
BTN_MENU = "🧭 Menu"
BTN_ADD_GLOBAL = "➕ Add Global Rule"
BTN_ADD_USER = "➕ Add User Rule"
BTN_LIST = "📌 List Rules"
BTN_DELETE = "➖ Delete Rule"
BTN_BACKUP = "☁️ Backup (Drive)"
BTN_RESTORE = "⬇️ Restore (Drive)"
BTN_SET_DRIVE_ID = "📎 Set Drive File ID"
BTN_CANCEL = "❌ Cancel"
BTN_HELP = "ℹ️ Help"

BTN_EXACT = "✅ Exact"
BTN_CONTAINS = "🔎 Contains"
BTN_REGEX = "🧩 Regex"

BTN_REPLY_TEXT = "✍️ Reply: Text"
BTN_REPLY_MEDIA = "📎 Reply: Media/File"

ADMIN_KB = ReplyKeyboardMarkup(
    [
        [BTN_MENU, BTN_HELP, BTN_CANCEL],
        [BTN_ADD_GLOBAL, BTN_ADD_USER],
        [BTN_LIST, BTN_DELETE],
        [BTN_BACKUP, BTN_RESTORE],
        [BTN_SET_DRIVE_ID],
    ],
    resize_keyboard=True
)

TYPE_KB = ReplyKeyboardMarkup(
    [
        [BTN_EXACT, BTN_CONTAINS, BTN_REGEX],
        [BTN_CANCEL],
    ],
    resize_keyboard=True
)

REPLY_KIND_KB = ReplyKeyboardMarkup(
    [
        [BTN_REPLY_TEXT, BTN_REPLY_MEDIA],
        [BTN_CANCEL],
    ],
    resize_keyboard=True
)

# ==================== ADMIN STATE ====================
@dataclass
class AState:
    state: str
    data: Dict[str, Any]

def get_admin_state(admin_id: int) -> AState:
    with db() as c:
        row = c.execute("SELECT state, data_json FROM admin_state WHERE admin_id=?", (admin_id,)).fetchone()
        if not row:
            return AState("idle", {})
        return AState(row[0], json.loads(row[1]))

def set_admin_state(admin_id: int, state: str, data: Dict[str, Any]):
    with db() as c:
        c.execute("""
        INSERT INTO admin_state(admin_id, state, data_json, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(admin_id) DO UPDATE SET
          state=excluded.state,
          data_json=excluded.data_json,
          updated_at=excluded.updated_at
        """, (admin_id, state, json.dumps(data, ensure_ascii=False), int(time.time())))
        c.commit()

def clear_admin_state(admin_id: int):
    set_admin_state(admin_id, "idle", {})

# ==================== RULE OPS ====================
def add_rule(scope: str, user_id: Optional[int], match_type: str, trigger: str,
             reply_kind: str, reply_text: Optional[str],
             reply_chat_id: Optional[int], reply_message_id: Optional[int]) -> int:
    created = int(time.time())
    trig = trigger
    if match_type in ("exact", "contains"):
        trig = normalize(trigger)

    if match_type == "regex":
        # validate regex now to avoid bad rules breaking runtime
        try:
            re.compile(trig)
        except re.error as e:
            raise ValueError(f"Invalid regex: {e}")

    with db() as c:
        cur = c.execute("""
        INSERT INTO rules(scope, user_id, match_type, trigger,
                          reply_kind, reply_text, reply_chat_id, reply_message_id, created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (scope, user_id, match_type, trig,
              reply_kind, reply_text, reply_chat_id, reply_message_id, created))
        c.commit()
        return int(cur.lastrowid)

def list_rules(limit: int = 80) -> List[Tuple]:
    with db() as c:
        return c.execute("""
        SELECT id, scope, user_id, match_type, trigger, reply_kind, created_at
        FROM rules
        ORDER BY id DESC
        LIMIT ?
        """, (limit,)).fetchall()

def delete_rule_by_id(rule_id: int) -> bool:
    with db() as c:
        cur = c.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        c.commit()
        return cur.rowcount > 0

def find_matching_rule(user_id: int, text: str) -> Optional[Tuple]:
    """
    Priority:
    user-scope -> global-scope
    Within a scope:
    exact -> contains -> regex
    Newer rules take priority for contains/regex (DESC order).
    Returns tuple:
      (id, match_type, trigger, reply_kind, reply_text, reply_chat_id, reply_message_id)
    """
    t_norm = normalize(text)
    if not t_norm:
        return None

    def try_scope(scope: str, uid: Optional[int]) -> Optional[Tuple]:
        with db() as c:
            # exact
            row = c.execute("""
            SELECT id, match_type, trigger, reply_kind, reply_text, reply_chat_id, reply_message_id
            FROM rules
            WHERE scope=? AND COALESCE(user_id,0)=COALESCE(?,0)
              AND match_type='exact' AND trigger=?
            ORDER BY id DESC LIMIT 1
            """, (scope, uid, t_norm)).fetchone()
            if row:
                return row

            # contains
            rows = c.execute("""
            SELECT id, match_type, trigger, reply_kind, reply_text, reply_chat_id, reply_message_id
            FROM rules
            WHERE scope=? AND COALESCE(user_id,0)=COALESCE(?,0)
              AND match_type='contains'
            ORDER BY id DESC
            LIMIT 300
            """, (scope, uid)).fetchall()
            for r in rows:
                trig = r[2]
                if trig and trig in t_norm:
                    return r

            # regex
            rows = c.execute("""
            SELECT id, match_type, trigger, reply_kind, reply_text, reply_chat_id, reply_message_id
            FROM rules
            WHERE scope=? AND COALESCE(user_id,0)=COALESCE(?,0)
              AND match_type='regex'
            ORDER BY id DESC
            LIMIT 300
            """, (scope, uid)).fetchall()
            for r in rows:
                pat = r[2]
                try:
                    if re.search(pat, text, flags=re.IGNORECASE):
                        return r
                except re.error:
                    continue
        return None

    r = try_scope("user", user_id)
    if r:
        return r
    return try_scope("global", None)

# ==================== LINKS (reply routing) ====================
def link_admin_msg(admin_msg_id: int, user_chat_id: int, user_id: int):
    with db() as c:
        c.execute("""
        INSERT OR REPLACE INTO links(admin_message_id, user_chat_id, user_id, created_at)
        VALUES(?,?,?,?)
        """, (admin_msg_id, user_chat_id, user_id, int(time.time())))
        c.commit()

def get_link(admin_msg_id: int) -> Optional[Tuple[int, int]]:
    with db() as c:
        row = c.execute("SELECT user_chat_id, user_id FROM links WHERE admin_message_id=?", (admin_msg_id,)).fetchone()
        return row if row else None

# ==================== GOOGLE DRIVE (SERVICE ACCOUNT) ====================
def gdrive_service():
    if not GOOGLE_DRIVE_JSON:
        raise RuntimeError("GOOGLE_DRIVE_JSON env not set")
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    info = json.loads(GOOGLE_DRIVE_JSON)
    creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def gdrive_upload_db() -> str:
    fid = effective_drive_file_id()
    if not fid:
        raise RuntimeError("No Drive file_id set (use ENV GDRIVE_FILE_ID or admin button 'Set Drive File ID').")

    service = gdrive_service()
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(DB_PATH, mimetype="application/x-sqlite3", resumable=True)
    updated = service.files().update(fileId=fid, media_body=media).execute()
    return updated["id"]

def gdrive_download_db(file_id: str):
    service = gdrive_service()
    request = service.files().get_media(fileId=file_id)

    from googleapiclient.http import MediaIoBaseDownload
    import io

    fh = io.FileIO(DB_PATH, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    fh.close()

# ==================== ADMIN COMMANDS (minimal) ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("✅ Admin panel ready.", reply_markup=ADMIN_KB)
    else:
        await update.message.reply_text("✅ Message भेजो—अगर rule match होगा तो auto-reply मिलेगा, नहीं तो admin तक चला जाएगा।")

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    fid = effective_drive_file_id()
    txt = (
        "Admin Panel:\n"
        "➕ Add Global Rule: सभी users\n"
        "➕ Add User Rule: specific user_id\n"
        "Rule types: Exact / Contains / Regex\n"
        "Reply: Text या Media/File (photo/video/doc/voice)\n\n"
        "Reply routing: admin forwarded/copied message पर Reply करेगा → user को चला जाएगा.\n\n"
        f"Drive file_id (effective): {fid if fid else '(not set)'}\n"
        "Note: अगर ENV GDRIVE_FILE_ID set है तो वही priority में रहेगा."
    )
    await update.message.reply_text(txt, reply_markup=ADMIN_KB)

async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    clear_admin_state(ADMIN_ID)
    await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_KB)

async def admin_add_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    set_admin_state(ADMIN_ID, "choose_type", {"scope": "global"})
    await update.message.reply_text("Rule type चुनो:", reply_markup=TYPE_KB)

async def admin_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    set_admin_state(ADMIN_ID, "ask_user_id", {"scope": "user"})
    await update.message.reply_text("जिस user के लिए rule बनानी है उसका user_id भेजो (number).", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = list_rules()
    if not rows:
        await update.message.reply_text("No rules yet.", reply_markup=ADMIN_KB)
        return
    lines = []
    for rid, scope, uid, mtype, trig, rkind, _ts in rows:
        tag = scope if scope == "global" else f"user:{uid}"
        lines.append(f"#{rid} | {tag} | {mtype} | {trig} | {rkind}")
    text = "📌 Rules (latest first):\n" + "\n".join(lines[:80])
    await update.message.reply_text(text, reply_markup=ADMIN_KB)

async def admin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    set_admin_state(ADMIN_ID, "delete_wait_id", {})
    await update.message.reply_text("Delete करने के लिए rule ID भेजो (जैसे: 12).", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))

async def admin_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        fid = gdrive_upload_db()
        await update.message.reply_text(f"☁️ Backup done. Updated file id: {fid}", reply_markup=ADMIN_KB)
    except Exception as e:
        await update.message.reply_text(f"❌ Backup error: {e}", reply_markup=ADMIN_KB)

async def admin_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    fid = effective_drive_file_id()
    if fid:
        try:
            gdrive_download_db(fid)
            clear_admin_state(ADMIN_ID)
            await update.message.reply_text("✅ Restored DB from Drive (saved file_id).", reply_markup=ADMIN_KB)
        except Exception as e:
            await update.message.reply_text(f"❌ Restore error: {e}", reply_markup=ADMIN_KB)
        return

    set_admin_state(ADMIN_ID, "restore_wait_fileid", {})
    await update.message.reply_text("Restore के लिए Drive file_id भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))

async def admin_set_drive_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    note = ""
    if ENV_GDRIVE_FILE_ID:
        note = (
            "⚠️ Note: ENV GDRIVE_FILE_ID set है, इसलिए वही priority में रहेगा.\n"
            "अगर chat से control चाहिए तो Railway ENV हटाओ.\n\n"
        )
    set_admin_state(ADMIN_ID, "wait_drive_file_id", {})
    await update.message.reply_text(
        note + "Drive file_id भेजो (जिस file में backup overwrite करना है):",
        reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
    )

# ==================== MAIN MESSAGE HANDLER ====================
async def handle_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    uid = update.effective_user.id

    # ---------------- ADMIN FLOW ----------------
    if uid == ADMIN_ID:
        # A) Reply routing: admin replies to copied message -> send to user
        if msg.reply_to_message:
            link = get_link(msg.reply_to_message.message_id)
            if link:
                user_chat_id, _user_id = link
                await context.bot.copy_message(
                    chat_id=user_chat_id,
                    from_chat_id=ADMIN_ID,
                    message_id=msg.message_id
                )
                return

        # B) Handle menu buttons
        if msg.text:
            t = msg.text.strip()
            if t == BTN_MENU:
                await msg.reply_text("🧭 Menu", reply_markup=ADMIN_KB)
                return
            if t == BTN_HELP:
                await admin_help(update, context)
                return
            if t == BTN_CANCEL:
                await admin_cancel(update, context)
                return
            if t == BTN_ADD_GLOBAL:
                await admin_add_global(update, context)
                return
            if t == BTN_ADD_USER:
                await admin_add_user(update, context)
                return
            if t == BTN_LIST:
                await admin_list(update, context)
                return
            if t == BTN_DELETE:
                await admin_delete(update, context)
                return
            if t == BTN_BACKUP:
                await admin_backup(update, context)
                return
            if t == BTN_RESTORE:
                await admin_restore(update, context)
                return
            if t == BTN_SET_DRIVE_ID:
                await admin_set_drive_id(update, context)
                return

        # C) Wizard states
        state = get_admin_state(ADMIN_ID)

        if state.state == "ask_user_id":
            if not msg.text:
                await msg.reply_text("user_id number में भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            try:
                user_id = int(msg.text.strip())
            except ValueError:
                await msg.reply_text("❌ valid number भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            set_admin_state(ADMIN_ID, "choose_type", {"scope": "user", "user_id": user_id})
            await msg.reply_text("Rule type चुनो:", reply_markup=TYPE_KB)
            return

        if state.state == "choose_type":
            if not msg.text:
                return
            t = msg.text.strip()
            if t in (BTN_EXACT, BTN_CONTAINS, BTN_REGEX):
                mtype = "exact" if t == BTN_EXACT else ("contains" if t == BTN_CONTAINS else "regex")
                data = dict(state.data)
                data["match_type"] = mtype
                set_admin_state(ADMIN_ID, "ask_trigger", data)
                await msg.reply_text(
                    "Trigger भेजो:\n- Exact: पूरी line/sentence\n- Contains: word/phrase\n- Regex: pattern",
                    reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
                )
                return
            await msg.reply_text("Type buttons में से चुनो.", reply_markup=TYPE_KB)
            return

        if state.state == "ask_trigger":
            if not msg.text:
                await msg.reply_text("Trigger text भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            data = dict(state.data)
            data["trigger"] = msg.text.strip()
            set_admin_state(ADMIN_ID, "choose_reply_kind", data)
            await msg.reply_text("Reply किस तरह save करनी है?", reply_markup=REPLY_KIND_KB)
            return

        if state.state == "choose_reply_kind":
            if not msg.text:
                return
            t = msg.text.strip()
            if t == BTN_REPLY_TEXT:
                data = dict(state.data)
                data["reply_kind"] = "text"
                set_admin_state(ADMIN_ID, "wait_reply_text", data)
                await msg.reply_text("अब reply TEXT भेजो:", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            if t == BTN_REPLY_MEDIA:
                data = dict(state.data)
                data["reply_kind"] = "copy"
                set_admin_state(ADMIN_ID, "wait_reply_media", data)
                await msg.reply_text("अब कोई भी media/file/message भेजो (photo/video/doc/voice/etc):", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            await msg.reply_text("Buttons से चुनो.", reply_markup=REPLY_KIND_KB)
            return

        if state.state == "wait_reply_text":
            if not msg.text:
                await msg.reply_text("Text reply भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            data = state.data
            try:
                rid = add_rule(
                    scope=data["scope"],
                    user_id=data.get("user_id"),
                    match_type=data["match_type"],
                    trigger=data["trigger"],
                    reply_kind="text",
                    reply_text=msg.text,
                    reply_chat_id=None,
                    reply_message_id=None
                )
            except Exception as e:
                clear_admin_state(ADMIN_ID)
                await msg.reply_text(f"❌ Save error: {e}", reply_markup=ADMIN_KB)
                return
            clear_admin_state(ADMIN_ID)
            await msg.reply_text(f"✅ Saved rule #{rid}", reply_markup=ADMIN_KB)
            return

        if state.state == "wait_reply_media":
            data = state.data
            try:
                rid = add_rule(
                    scope=data["scope"],
                    user_id=data.get("user_id"),
                    match_type=data["match_type"],
                    trigger=data["trigger"],
                    reply_kind="copy",
                    reply_text=None,
                    reply_chat_id=ADMIN_ID,
                    reply_message_id=msg.message_id
                )
            except Exception as e:
                clear_admin_state(ADMIN_ID)
                await msg.reply_text(f"❌ Save error: {e}", reply_markup=ADMIN_KB)
                return
            clear_admin_state(ADMIN_ID)
            await msg.reply_text(f"✅ Saved media rule #{rid}", reply_markup=ADMIN_KB)
            return

        if state.state == "delete_wait_id":
            if not msg.text:
                return
            try:
                rid = int(msg.text.strip())
            except ValueError:
                await msg.reply_text("❌ ID number भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            ok = delete_rule_by_id(rid)
            clear_admin_state(ADMIN_ID)
            await msg.reply_text("✅ Deleted." if ok else "❌ Not found.", reply_markup=ADMIN_KB)
            return

        if state.state == "restore_wait_fileid":
            if not msg.text:
                return
            file_id = msg.text.strip()
            try:
                gdrive_download_db(file_id)
                clear_admin_state(ADMIN_ID)
                await msg.reply_text("✅ Restored DB from Drive.", reply_markup=ADMIN_KB)
            except Exception as e:
                await msg.reply_text(f"❌ Restore error: {e}", reply_markup=ADMIN_KB)
            return

        if state.state == "wait_drive_file_id":
            if not msg.text:
                return
            fid = msg.text.strip()
            if len(fid) < 10:
                await msg.reply_text("❌ file_id सही नहीं लग रहा. फिर से भेजो.", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
                return
            set_setting("drive_file_id", fid)
            clear_admin_state(ADMIN_ID)
            await msg.reply_text(f"✅ Saved Drive file_id in DB:\n{fid}", reply_markup=ADMIN_KB)
            return

        return  # admin idle

    # ---------------- USER FLOW ----------------
    # 1) If text -> try auto-reply
    if msg.text:
        rule = find_matching_rule(uid, msg.text)
        if rule:
            _id, _mtype, _trig, rkind, rtext, rchat, rmid = rule
            if rkind == "text":
                await msg.reply_text(rtext)
                return
            if rkind == "copy":
                await context.bot.copy_message(
                    chat_id=msg.chat_id,
                    from_chat_id=rchat,
                    message_id=rmid
                )
                return

    # 2) Otherwise send to admin (copy message + link for reply routing)
    header = (
        f"📩 From: {update.effective_user.full_name} @{(update.effective_user.username or '')}\n"
        f"🆔 user_id: {uid}\n"
        f"💬 chat_id: {msg.chat_id}"
    )
    header_msg = await context.bot.send_message(chat_id=ADMIN_ID, text=header)

    copied = await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id
    )

    link_admin_msg(header_msg.message_id, msg.chat_id, uid)
    link_admin_msg(copied.message_id, msg.chat_id, uid)

# ==================== ENTRY ====================
def main():
    init_db()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", admin_help))  # admin use only
    app.add_handler(MessageHandler(filters.ALL, handle_all))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
