# bot_propp_sqlite.py
# Pro++ Professional Support Service Bot (No money features)
# SQLite storage + Admin menus + Live chat + Regex auto replies (multi-part + buttons) + KB + Broadcast + Analytics
#
# ENV:
#   BOT_TOKEN=...
#   ADMIN_IDS=7988263992,123...   (optional; used for first-time bootstrap)
#   OWNER_ID=7988263992          (optional; overrides owner bootstrap)
#   DB_PATH=bot.db               (optional; default ./bot.db)
#
# WEBHOOK (optional):
#   APP_URL / RENDER_EXTERNAL_URL / RAILWAY_PUBLIC_DOMAIN / RAILWAY_STATIC_URL
#   PORT (platform provided)

import os
import re
import json
import time
import sqlite3
from collections import Counter
from typing import Optional, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------- CONFIG ---------------------

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in env")

DB_PATH = (os.getenv("DB_PATH") or "bot.db").strip()

BOOTSTRAP_ADMIN_IDS = [int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip().isdigit()]
OWNER_ID_ENV = os.getenv("OWNER_ID")
OWNER_ID = int(OWNER_ID_ENV) if (OWNER_ID_ENV and OWNER_ID_ENV.strip().isdigit()) else (BOOTSTRAP_ADMIN_IDS[0] if BOOTSTRAP_ADMIN_IDS else 7988263992)

# --------------------- DB HELPERS ---------------------

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con

def init_db() -> None:
    con = _conn()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_seen INTEGER,
        current_ticket_id INTEGER
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL CHECK(role IN ('owner','admin','agent')),
        added_at INTEGER NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        status TEXT NOT NULL CHECK(status IN ('open','pending','solved','closed')) DEFAULT 'open',
        priority TEXT NOT NULL CHECK(priority IN ('low','medium','high')) DEFAULT 'medium',
        assigned_to INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        closed_at INTEGER,
        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ticket_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        from_admin INTEGER NOT NULL DEFAULT 0,
        sender_id INTEGER NOT NULL,
        msg_type TEXT NOT NULL CHECK(msg_type IN ('text','photo','video','document')),
        text TEXT,
        file_id TEXT,
        caption TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS auto_replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        payload TEXT NOT NULL,     -- JSON array of parts
        buttons TEXT NOT NULL,     -- JSON rows of url buttons
        created_at INTEGER NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS kb_articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        tags TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broadcasts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        payload TEXT NOT NULL,   -- JSON array of parts
        buttons TEXT NOT NULL,   -- JSON rows of url buttons
        created_at INTEGER NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        broadcast_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        ok INTEGER NOT NULL,
        error TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(broadcast_id) REFERENCES broadcasts(id) ON DELETE CASCADE
    );
    """)

    con.commit()
    cur.close()
    con.close()

    bootstrap_admins()

def bootstrap_admins() -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM admins;")
    c = cur.fetchone()["c"]
    now = int(time.time())

    if c == 0:
        # Owner
        cur.execute("INSERT OR REPLACE INTO admins(user_id, role, added_at) VALUES(?,?,?)", (OWNER_ID, "owner", now))
        # Other bootstrap admins become "admin"
        for aid in BOOTSTRAP_ADMIN_IDS:
            if aid == OWNER_ID:
                continue
            cur.execute("INSERT OR IGNORE INTO admins(user_id, role, added_at) VALUES(?,?,?)", (aid, "admin", now))

    con.commit()
    cur.close()
    con.close()

def upsert_user(u) -> None:
    con = _conn()
    cur = con.cursor()
    now = int(time.time())
    cur.execute("""
        INSERT INTO users(user_id, username, first_name, last_seen)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen
    """, (u.id, u.username or "", u.first_name or "", now))
    con.commit()
    cur.close()
    con.close()

def set_user_current_ticket(user_id: int, ticket_id: Optional[int]) -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute("UPDATE users SET current_ticket_id=? WHERE user_id=?", (ticket_id, user_id))
    con.commit()
    cur.close()
    con.close()

def get_user_current_ticket(user_id: int) -> Optional[int]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT current_ticket_id FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    if not row:
        return None
    return row["current_ticket_id"]

def is_admin(user_id: int) -> bool:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT 1 FROM admins WHERE user_id=? LIMIT 1", (user_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    return row is not None

def get_role(user_id: int) -> Optional[str]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT role FROM admins WHERE user_id=? LIMIT 1", (user_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    return row["role"] if row else None

def require_admin(user_id: int) -> bool:
    return is_admin(user_id)

def require_owner(user_id: int) -> bool:
    return get_role(user_id) == "owner"

# --------------------- BUTTONS / MARKUP ---------------------

def parse_buttons_text(raw: str) -> list:
    """
    Each line = one row.
    Multiple buttons in a row separated by ';'
    Formats: Text=https://...  OR  Text - https://...
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    rows = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = [p.strip() for p in ln.split(";") if p.strip()]
        row = []
        for p in parts:
            t = u = ""
            if "=" in p:
                t, u = p.split("=", 1)
            elif " - " in p:
                t, u = p.split(" - ", 1)
            t, u = t.strip(), u.strip()
            if not t or not u:
                continue
            if not (u.startswith("http://") or u.startswith("https://") or u.startswith("tg://")):
                continue
            row.append({"text": t, "url": u})
        if row:
            rows.append(row)
    return rows

def build_buttons_markup(button_rows: list) -> Optional[InlineKeyboardMarkup]:
    if not button_rows:
        return None
    kb = []
    for row in button_rows:
        btn_row = []
        for b in row or []:
            if isinstance(b, dict) and b.get("text") and b.get("url"):
                btn_row.append(InlineKeyboardButton(b["text"], url=b["url"]))
        if btn_row:
            kb.append(btn_row)
    return InlineKeyboardMarkup(kb) if kb else None

def safe_json_load(s: str, default: Any):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

# --------------------- APP URL (WEBHOOK) ---------------------

def get_app_url() -> str:
    # Manual override first
    url = (os.getenv("APP_URL") or "").strip()
    if url:
        return url

    # Render
    url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    if url:
        return url

    # Railway
    dom = (os.getenv("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if dom:
        return f"https://{dom}"

    static_url = (os.getenv("RAILWAY_STATIC_URL") or "").strip()
    if static_url:
        return static_url if static_url.startswith("http") else f"https://{static_url}"

    return ""

# --------------------- MENUS ---------------------

def user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 Create Ticket", callback_data="U_NEW_TICKET")],
        [InlineKeyboardButton("📚 Help Center", callback_data="U_HELP_CENTER")],
        [InlineKeyboardButton("📂 My Tickets", callback_data="U_MY_TICKETS")],
    ])

def ticket_priority_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Low", callback_data="U_TPR_LOW"),
         InlineKeyboardButton("🟡 Medium", callback_data="U_TPR_MED"),
         InlineKeyboardButton("🔴 High", callback_data="U_TPR_HIGH")],
        [InlineKeyboardButton("🔙 Back", callback_data="U_BACK_HOME")]
    ])

def admin_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 Tickets", callback_data="A_TICKETS")],
        [InlineKeyboardButton("💬 Live Chat", callback_data="A_CHAT_HELP")],
        [InlineKeyboardButton("🤖 Auto Replies", callback_data="A_AR")],
        [InlineKeyboardButton("📚 Knowledge Base", callback_data="A_KB")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="A_BC")],
        [InlineKeyboardButton("📊 Analytics", callback_data="A_STATS")],
        [InlineKeyboardButton("👥 Team/Roles", callback_data="A_TEAM")],
    ])

def admin_ar_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add (Wizard)", callback_data="A_AR_ADD")],
        [InlineKeyboardButton("📋 List", callback_data="A_AR_LIST")],
        [InlineKeyboardButton("🔙 Back", callback_data="A_BACK_ADMIN")],
    ])

def admin_kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Article", callback_data="A_KB_ADD")],
        [InlineKeyboardButton("📋 List", callback_data="A_KB_LIST")],
        [InlineKeyboardButton("🔙 Back", callback_data="A_BACK_ADMIN")],
    ])

def admin_ticket_actions(ticket_id: int, assigned_to: Optional[int], status: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("💬 Chat Mode", callback_data=f"A_CHAT_{ticket_id}")],
        [
            InlineKeyboardButton("🧑‍💻 Assign to me", callback_data=f"A_ASSIGN_{ticket_id}"),
            InlineKeyboardButton("✅ Solve", callback_data=f"A_STATUS_{ticket_id}_solved"),
        ],
        [
            InlineKeyboardButton("⏳ Pending", callback_data=f"A_STATUS_{ticket_id}_pending"),
            InlineKeyboardButton("🔒 Close", callback_data=f"A_STATUS_{ticket_id}_closed"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="A_TICKETS")],
    ]
    return InlineKeyboardMarkup(buttons)

def admin_team_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Admin/Agent", callback_data="A_TEAM_ADD")],
        [InlineKeyboardButton("📋 List Team", callback_data="A_TEAM_LIST")],
        [InlineKeyboardButton("🔙 Back", callback_data="A_BACK_ADMIN")],
    ])

def admin_bc_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 New Broadcast (Wizard)", callback_data="A_BC_NEW")],
        [InlineKeyboardButton("🔙 Back", callback_data="A_BACK_ADMIN")],
    ])

# --------------------- TICKET OPS ---------------------

def create_ticket(user, text: str, priority: str) -> int:
    now = int(time.time())
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO tickets(user_id, username, first_name, status, priority, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?)
    """, (user.id, user.username or "", user.first_name or "", "open", priority, now, now))
    tid = cur.lastrowid
    cur.execute("""
        INSERT INTO ticket_messages(ticket_id, from_admin, sender_id, msg_type, text, created_at)
        VALUES(?,?,?,?,?,?)
    """, (tid, 0, user.id, "text", text, now))
    con.commit()
    cur.close()
    con.close()
    return int(tid)

def add_ticket_message(ticket_id: int, from_admin: bool, sender_id: int, msg_type: str,
                       text: Optional[str] = None, file_id: Optional[str] = None, caption: Optional[str] = None) -> None:
    now = int(time.time())
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO ticket_messages(ticket_id, from_admin, sender_id, msg_type, text, file_id, caption, created_at)
        VALUES(?,?,?,?,?,?,?,?)
    """, (ticket_id, 1 if from_admin else 0, sender_id, msg_type, text, file_id, caption, now))
    cur.execute("UPDATE tickets SET updated_at=? WHERE id=?", (now, ticket_id))
    con.commit()
    cur.close()
    con.close()

def get_ticket(ticket_id: int) -> Optional[sqlite3.Row]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    return row

def list_user_tickets(user_id: int, limit: int = 10) -> list:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT id, status, priority, created_at, updated_at
        FROM tickets
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows

def list_open_tickets(limit: int = 25) -> list:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT id, user_id, username, first_name, status, priority, assigned_to, created_at, updated_at
        FROM tickets
        WHERE status IN ('open','pending')
        ORDER BY priority='high' DESC, updated_at DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows

def assign_ticket(ticket_id: int, admin_id: int) -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute("UPDATE tickets SET assigned_to=?, updated_at=? WHERE id=?", (admin_id, int(time.time()), ticket_id))
    con.commit()
    cur.close()
    con.close()

def set_ticket_status(ticket_id: int, status: str) -> None:
    now = int(time.time())
    con = _conn()
    cur = con.cursor()
    closed_at = now if status in ("solved", "closed") else None
    cur.execute("UPDATE tickets SET status=?, closed_at=?, updated_at=? WHERE id=?", (status, closed_at, now, ticket_id))
    con.commit()
    cur.close()
    con.close()

def get_ticket_messages(ticket_id: int, limit: int = 8) -> list:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT from_admin, msg_type, text, caption, created_at
        FROM ticket_messages
        WHERE ticket_id=?
        ORDER BY id DESC
        LIMIT ?
    """, (ticket_id, limit))
    rows = cur.fetchall()
    cur.close()
    con.close()
    return list(reversed(rows))

# --------------------- AUTO REPLY OPS ---------------------

def add_auto_reply(pattern: str, priority: int, payload: list, buttons: list) -> int:
    now = int(time.time())
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO auto_replies(pattern, priority, enabled, payload, buttons, created_at)
        VALUES(?,?,?,?,?,?)
    """, (pattern, priority, 1, json.dumps(payload, ensure_ascii=False), json.dumps(buttons, ensure_ascii=False), now))
    rid = cur.lastrowid
    con.commit()
    cur.close()
    con.close()
    return int(rid)

def list_auto_replies(limit: int = 50) -> list:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT id, pattern, priority, enabled, created_at
        FROM auto_replies
        ORDER BY priority DESC, id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows

def toggle_auto_reply(rule_id: int) -> Optional[int]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT enabled FROM auto_replies WHERE id=?", (rule_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); con.close()
        return None
    new_val = 0 if row["enabled"] else 1
    cur.execute("UPDATE auto_replies SET enabled=? WHERE id=?", (new_val, rule_id))
    con.commit()
    cur.close()
    con.close()
    return new_val

def delete_auto_reply(rule_id: int) -> bool:
    con = _conn()
    cur = con.cursor()
    cur.execute("DELETE FROM auto_replies WHERE id=?", (rule_id,))
    ok = cur.rowcount > 0
    con.commit()
    cur.close()
    con.close()
    return ok

def find_auto_reply(user_text: str) -> Optional[dict]:
    text = (user_text or "").strip()
    if not text:
        return None
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT id, pattern, priority, payload, buttons
        FROM auto_replies
        WHERE enabled=1
        ORDER BY priority DESC, id ASC
    """)
    rules = cur.fetchall()
    cur.close()
    con.close()

    for r in rules:
        try:
            if re.search(r["pattern"], text, flags=re.IGNORECASE):
                return {
                    "id": r["id"],
                    "payload": safe_json_load(r["payload"], []),
                    "buttons": safe_json_load(r["buttons"], [])
                }
        except re.error:
            continue
    return None

# --------------------- KB OPS ---------------------

def add_kb_article(title: str, content: str, tags: str) -> int:
    now = int(time.time())
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO kb_articles(title, content, tags, enabled, created_at)
        VALUES(?,?,?,?,?)
    """, (title, content, tags, 1, now))
    aid = cur.lastrowid
    con.commit()
    cur.close()
    con.close()
    return int(aid)

def list_kb_articles(limit: int = 50) -> list:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT id, title, tags, enabled, created_at
        FROM kb_articles
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows

def toggle_kb_article(article_id: int) -> Optional[int]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT enabled FROM kb_articles WHERE id=?", (article_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); con.close()
        return None
    new_val = 0 if row["enabled"] else 1
    cur.execute("UPDATE kb_articles SET enabled=? WHERE id=?", (new_val, article_id))
    con.commit()
    cur.close()
    con.close()
    return new_val

def delete_kb_article(article_id: int) -> bool:
    con = _conn()
    cur = con.cursor()
    cur.execute("DELETE FROM kb_articles WHERE id=?", (article_id,))
    ok = cur.rowcount > 0
    con.commit()
    cur.close()
    con.close()
    return ok

def search_kb(query: str, limit: int = 10) -> list:
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q.lower()}%"
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT id, title
        FROM kb_articles
        WHERE enabled=1 AND (
            lower(title) LIKE ? OR lower(content) LIKE ? OR lower(tags) LIKE ?
        )
        ORDER BY id DESC
        LIMIT ?
    """, (like, like, like, limit))
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows

def get_kb_article(article_id: int) -> Optional[sqlite3.Row]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM kb_articles WHERE id=? AND enabled=1", (article_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    return row

# --------------------- BROADCAST OPS ---------------------

def create_broadcast(admin_id: int, payload: list, buttons: list) -> int:
    now = int(time.time())
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO broadcasts(admin_id, payload, buttons, created_at)
        VALUES(?,?,?,?)
    """, (admin_id, json.dumps(payload, ensure_ascii=False), json.dumps(buttons, ensure_ascii=False), now))
    bid = cur.lastrowid
    con.commit()
    cur.close()
    con.close()
    return int(bid)

def log_broadcast(broadcast_id: int, user_id: int, ok: bool, error: str = "") -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO broadcast_logs(broadcast_id, user_id, ok, error, created_at)
        VALUES(?,?,?,?,?)
    """, (broadcast_id, user_id, 1 if ok else 0, error[:300], int(time.time())))
    con.commit()
    cur.close()
    con.close()

def list_all_users() -> list[int]:
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT user_id FROM users ORDER BY user_id ASC")
    rows = cur.fetchall()
    cur.close()
    con.close()
    return [int(r["user_id"]) for r in rows]

# --------------------- ANALYTICS ---------------------

def analytics_summary() -> str:
    con = _conn()
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) AS c FROM users")
    users_count = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'")
    open_c = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='pending'")
    pend_c = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='solved'")
    solved_c = cur.fetchone()["c"]

    # Avg first response time (minutes)
    # For each ticket, find first admin message time and created_at
    cur.execute("""
        SELECT t.id, t.created_at,
               (SELECT MIN(created_at) FROM ticket_messages m WHERE m.ticket_id=t.id AND m.from_admin=1) AS first_admin
        FROM tickets t
        WHERE t.created_at > ?
        ORDER BY t.id DESC
        LIMIT 200
    """, (int(time.time()) - 30 * 86400,))
    rows = cur.fetchall()
    deltas = []
    for r in rows:
        if r["first_admin"]:
            deltas.append((r["first_admin"] - r["created_at"]) / 60.0)
    avg_first = (sum(deltas) / len(deltas)) if deltas else None

    # Top keywords (last 500 user text messages)
    cur.execute("""
        SELECT text FROM ticket_messages
        WHERE from_admin=0 AND msg_type='text' AND text IS NOT NULL
        ORDER BY id DESC
        LIMIT 500
    """)
    texts = [x["text"] for x in cur.fetchall() if x["text"]]
    cur.close()
    con.close()

    words = []
    for t in texts:
        for w in re.findall(r"[A-Za-z]{3,}", t.lower()):
            if w in {"this","that","with","have","your","from","please","help","problem"}:
                continue
            words.append(w)
    top = Counter(words).most_common(6)

    out = [
        "📊 *Analytics*",
        f"👥 Users: *{users_count}*",
        f"🎫 Tickets — Open: *{open_c}* | Pending: *{pend_c}* | Solved: *{solved_c}*",
    ]
    if avg_first is not None:
        out.append(f"⏱ Avg first response: *{avg_first:.1f} min* (last 30d sample)")
    if top:
        out.append("🔥 Top keywords: " + ", ".join([f"`{w}`({c})" for w, c in top]))
    return "\n".join(out)

# --------------------- SEND HELPERS ---------------------

async def send_payload(bot, chat_id: int, payload: list, buttons: list | None = None, fallback_menu: InlineKeyboardMarkup | None = None):
    payload = payload or []
    buttons = buttons or []
    markup = build_buttons_markup(buttons)
    if not markup:
        markup = fallback_menu

    last_i = len(payload) - 1
    for i, part in enumerate(payload):
        ptype = (part.get("type") or "text").lower()
        is_last = (i == last_i)
        rm = markup if is_last else None

        if ptype == "text":
            await bot.send_message(chat_id, part.get("text", ""), parse_mode="Markdown", reply_markup=rm)
        elif ptype == "photo":
            await bot.send_photo(chat_id, part.get("file_id"), caption=part.get("caption", ""), reply_markup=rm)
        elif ptype == "video":
            await bot.send_video(chat_id, part.get("file_id"), caption=part.get("caption", ""), reply_markup=rm)
        elif ptype == "document":
            await bot.send_document(chat_id, part.get("file_id"), caption=part.get("caption", ""), reply_markup=rm)

# --------------------- HANDLERS ---------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    if is_admin(u.id):
        await update.effective_message.reply_text("👑 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_main_menu())
    else:
        await update.effective_message.reply_text("👋 *Support Center*\nChoose an option:", parse_mode="Markdown", reply_markup=user_menu())

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    if not require_admin(u.id):
        await update.effective_message.reply_text("❌ Admin only.")
        return
    await update.effective_message.reply_text("👑 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_main_menu())

# ---------- CALLBACKS ----------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    u = update.effective_user
    upsert_user(u)
    await q.answer()
    data = q.data or ""

    # ---- USER ----
    if data == "U_BACK_HOME":
        await q.message.reply_text("🏠 Home", reply_markup=user_menu())
        return

    if data == "U_NEW_TICKET":
        context.user_data["mode"] = "new_ticket_pick_priority"
        await q.message.reply_text("Choose ticket priority:", reply_markup=ticket_priority_menu())
        return

    if data in ("U_TPR_LOW", "U_TPR_MED", "U_TPR_HIGH"):
        pr = {"U_TPR_LOW": "low", "U_TPR_MED": "medium", "U_TPR_HIGH": "high"}[data]
        context.user_data["new_ticket_priority"] = pr
        context.user_data["mode"] = "new_ticket_await_text"
        await q.message.reply_text("✍️ Send your issue details (text / photo / video / document).")
        return

    if data == "U_MY_TICKETS":
        rows = list_user_tickets(u.id, 10)
        if not rows:
            await q.message.reply_text("📭 No tickets yet.", reply_markup=user_menu())
            return
        lines = ["📂 *Your Tickets* (last 10):"]
        for r in rows:
            t = time.strftime("%d-%b %H:%M", time.localtime(r["created_at"]))
            lines.append(f"• `#{r['id']}` — *{r['status']}* — {r['priority']} — {t}")
        lines.append("\nTo continue a ticket, send: `/ticket <id>`")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=user_menu())
        return

    if data == "U_HELP_CENTER":
        context.user_data["mode"] = "kb_search"
        await q.message.reply_text("🔎 Type your keyword (example: `otp`, `login`, `error`).", parse_mode="Markdown")
        return

    if data.startswith("U_KB_OPEN_"):
        aid = int(data.split("_")[-1])
        art = get_kb_article(aid)
        if not art:
            await q.message.reply_text("❌ Article not found / disabled.")
            return
        txt = f"📚 *{art['title']}*\n\n{art['content']}\n\n_Tags:_ {art['tags']}"
        await q.message.reply_text(txt, parse_mode="Markdown", reply_markup=user_menu())
        return

    # ---- ADMIN ----
    if data == "A_BACK_ADMIN":
        if not require_admin(u.id):
            return
        await q.message.reply_text("👑 Admin Panel", reply_markup=admin_main_menu())
        return

    if data == "A_TICKETS":
        if not require_admin(u.id):
            return
        rows = list_open_tickets(25)
        if not rows:
            await q.message.reply_text("✅ No open/pending tickets.", reply_markup=admin_main_menu())
            return
        kb = []
        for r in rows:
            who = f"{r['first_name'] or '-'} (@{r['username'] or '-'})"
            label = f"#{r['id']} • {r['priority']} • {r['status']} • {who}"
            kb.append([InlineKeyboardButton(label, callback_data=f"A_OPEN_{r['id']}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="A_BACK_ADMIN")])
        await q.message.reply_text("🎫 *Open/Pending Tickets:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("A_OPEN_"):
        if not require_admin(u.id):
            return
        tid = int(data.split("_")[-1])
        t = get_ticket(tid)
        if not t:
            await q.message.reply_text("❌ Ticket not found.")
            return
        msgs = get_ticket_messages(tid, limit=8)
        lines = [
            f"🎫 *Ticket #{tid}*",
            f"User: `{t['user_id']}` @{t['username'] or '-'} ({t['first_name'] or '-'})",
            f"Status: *{t['status']}* | Priority: *{t['priority']}*",
            f"Assigned: `{t['assigned_to']}`" if t["assigned_to"] else "Assigned: _none_",
            "",
            "*Last messages:*"
        ]
        for m in msgs:
            who = "🛠 Admin" if m["from_admin"] else "👤 User"
            ts = time.strftime("%d-%b %H:%M", time.localtime(m["created_at"]))
            if m["msg_type"] == "text":
                preview = (m["text"] or "")[:180]
                lines.append(f"{who} [{ts}]: {preview}")
            else:
                lines.append(f"{who} [{ts}]: ({m['msg_type']}) {m['caption'] or ''}")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                   reply_markup=admin_ticket_actions(tid, t["assigned_to"], t["status"]))
        return

    if data.startswith("A_ASSIGN_"):
        if not require_admin(u.id):
            return
        tid = int(data.split("_")[-1])
        assign_ticket(tid, u.id)
        await q.message.reply_text(f"✅ Assigned Ticket #{tid} to you.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Open Ticket", callback_data=f"A_OPEN_{tid}")],
            [InlineKeyboardButton("🔙 Back", callback_data="A_TICKETS")]
        ]))
        return

    if data.startswith("A_STATUS_"):
        if not require_admin(u.id):
            return
        # A_STATUS_<id>_<status>
        parts = data.split("_")
        tid = int(parts[2])
        st = parts[3]
        set_ticket_status(tid, st)
        await q.message.reply_text(f"✅ Ticket #{tid} status -> *{st}*.", parse_mode="Markdown",
                                   reply_markup=InlineKeyboardMarkup([
                                       [InlineKeyboardButton("Open Ticket", callback_data=f"A_OPEN_{tid}")],
                                       [InlineKeyboardButton("🔙 Back", callback_data="A_TICKETS")]
                                   ]))
        return

    if data.startswith("A_CHAT_") and data != "A_CHAT_HELP":
        if not require_admin(u.id):
            return
        tid = int(data.split("_")[-1])
        t = get_ticket(tid)
        if not t:
            await q.message.reply_text("❌ Ticket not found.")
            return
        context.user_data["chat_ticket_id"] = tid
        await q.message.reply_text(
            f"💬 *Chat Mode ON* for Ticket #{tid}\n"
            "Now send messages (text/photo/video/doc). Send /endchat to stop.",
            parse_mode="Markdown"
        )
        return

    if data == "A_CHAT_HELP":
        if not require_admin(u.id):
            return
        await q.message.reply_text(
            "💬 *Live Chat*\n\n"
            "1) Open a ticket from *Tickets*\n"
            "2) Tap *Chat Mode*\n"
            "3) Send messages directly\n"
            "4) Stop with /endchat",
            parse_mode="Markdown",
            reply_markup=admin_main_menu()
        )
        return

    # Auto Replies
    if data == "A_AR":
        if not require_admin(u.id):
            return
        await q.message.reply_text("🤖 *Auto Replies*", parse_mode="Markdown", reply_markup=admin_ar_menu())
        return

    if data == "A_AR_ADD":
        if not require_admin(u.id):
            return
        context.user_data["ar_wz"] = {"step": "pattern", "payload": [], "buttons": [], "priority": 0}
        await q.message.reply_text("Send *trigger regex*.\nExample: `^sir ye problem hai$`", parse_mode="Markdown")
        return

    if data == "A_AR_LIST":
        if not require_admin(u.id):
            return
        rows = list_auto_replies(50)
        if not rows:
            await q.message.reply_text("No auto replies yet.", reply_markup=admin_ar_menu())
            return
        lines = ["🤖 *Auto Replies (top 50)*"]
        kb = []
        for r in rows[:20]:
            st = "✅" if r["enabled"] else "❌"
            lines.append(f"• ID `{r['id']}` {st} pr={r['priority']}  — `{r['pattern'][:40]}`")
        kb.append([InlineKeyboardButton("Toggle Rule", callback_data="A_AR_TOGGLE")])
        kb.append([InlineKeyboardButton("Delete Rule", callback_data="A_AR_DELETE")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="A_AR")])
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "A_AR_TOGGLE":
        if not require_admin(u.id):
            return
        context.user_data["mode"] = "ar_toggle"
        await q.message.reply_text("Send Rule ID to toggle (example: `3`)", parse_mode="Markdown")
        return

    if data == "A_AR_DELETE":
        if not require_admin(u.id):
            return
        context.user_data["mode"] = "ar_delete"
        await q.message.reply_text("Send Rule ID to delete (example: `3`)", parse_mode="Markdown")
        return

    # Knowledge Base
    if data == "A_KB":
        if not require_admin(u.id):
            return
        await q.message.reply_text("📚 *Knowledge Base*", parse_mode="Markdown", reply_markup=admin_kb_menu())
        return

    if data == "A_KB_ADD":
        if not require_admin(u.id):
            return
        context.user_data["kb_wz"] = {"step": "title"}
        await q.message.reply_text("Send article *title*.", parse_mode="Markdown")
        return

    if data == "A_KB_LIST":
        if not require_admin(u.id):
            return
        rows = list_kb_articles(50)
        if not rows:
            await q.message.reply_text("No KB articles yet.", reply_markup=admin_kb_menu())
            return
        lines = ["📚 *KB Articles (top 50)*"]
        kb = []
        for r in rows[:20]:
            st = "✅" if r["enabled"] else "❌"
            lines.append(f"• ID `{r['id']}` {st} — *{r['title']}*  _({r['tags']})_")
        kb.append([InlineKeyboardButton("Toggle Article", callback_data="A_KB_TOGGLE")])
        kb.append([InlineKeyboardButton("Delete Article", callback_data="A_KB_DELETE")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="A_KB")])
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data == "A_KB_TOGGLE":
        if not require_admin(u.id):
            return
        context.user_data["mode"] = "kb_toggle"
        await q.message.reply_text("Send Article ID to toggle (example: `2`)", parse_mode="Markdown")
        return

    if data == "A_KB_DELETE":
        if not require_admin(u.id):
            return
        context.user_data["mode"] = "kb_delete"
        await q.message.reply_text("Send Article ID to delete (example: `2`)", parse_mode="Markdown")
        return

    # Broadcast
    if data == "A_BC":
        if not require_admin(u.id):
            return
        await q.message.reply_text("📢 *Broadcast*", parse_mode="Markdown", reply_markup=admin_bc_menu())
        return

    if data == "A_BC_NEW":
        if not require_admin(u.id):
            return
        context.user_data["bc_wz"] = {"step": "collect", "payload": [], "buttons": []}
        await q.message.reply_text("Send broadcast content (text/photo/video/doc). Send DONE when finished.", parse_mode="Markdown")
        return

    # Analytics
    if data == "A_STATS":
        if not require_admin(u.id):
            return
        await q.message.reply_text(analytics_summary(), parse_mode="Markdown", reply_markup=admin_main_menu())
        return

    # Team/Roles
    if data == "A_TEAM":
        if not require_admin(u.id):
            return
        await q.message.reply_text("👥 *Team / Roles*", parse_mode="Markdown", reply_markup=admin_team_menu())
        return

    if data == "A_TEAM_LIST":
        if not require_admin(u.id):
            return
        con = _conn()
        cur = con.cursor()
        cur.execute("SELECT user_id, role, added_at FROM admins ORDER BY role='owner' DESC, role='admin' DESC, user_id ASC")
        rows = cur.fetchall()
        cur.close()
        con.close()
        lines = ["👥 *Team Members*"]
        for r in rows:
            ts = time.strftime("%d-%b %H:%M", time.localtime(r["added_at"]))
            lines.append(f"• `{r['user_id']}` — *{r['role']}* — {ts}")
        lines.append("\nOwner can change role using:\n`/setrole <user_id> <owner|admin|agent>`")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=admin_team_menu())
        return

    if data == "A_TEAM_ADD":
        if not require_admin(u.id):
            return
        if not require_owner(u.id):
            await q.message.reply_text("❌ Only owner can add roles.", reply_markup=admin_team_menu())
            return
        context.user_data["mode"] = "team_add"
        await q.message.reply_text("Send: `<user_id> <admin|agent>`\nExample: `123456789 agent`", parse_mode="Markdown")
        return

# ---------- TEXT / MEDIA ROUTER ----------

async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)

    msg = update.effective_message

    # --- Admin wizard: Auto Reply ---
    if require_admin(u.id) and context.user_data.get("ar_wz"):
        wz = context.user_data["ar_wz"]
        step = wz.get("step")

        if step == "pattern":
            pat = (msg.text or "").strip()
            try:
                re.compile(pat)
            except re.error:
                await msg.reply_text("❌ Invalid regex. Send again.")
                return
            wz["pattern"] = pat
            wz["step"] = "priority"
            await msg.reply_text("Send priority (0-100). Example: `50`", parse_mode="Markdown")
            return

        if step == "priority":
            raw = (msg.text or "0").strip()
            if not raw.lstrip("-").isdigit():
                await msg.reply_text("❌ Send a number (0-100).")
                return
            wz["priority"] = int(raw)
            wz["step"] = "collect"
            await msg.reply_text("Now send reply parts (text/photo/video/doc). Send DONE when finished.")
            return

        if step == "collect":
            if (msg.text or "").strip().upper() == "DONE":
                wz["step"] = "buttons"
                await msg.reply_text(
                    "Send buttons (optional) like:\n"
                    "Support=https://t.me/xyz\n"
                    "Website=https://example.com\n\n"
                    "Or type SKIP",
                    parse_mode="Markdown"
                )
                return

            if msg.photo:
                wz["payload"].append({"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption or ""})
            elif msg.video:
                wz["payload"].append({"type": "video", "file_id": msg.video.file_id, "caption": msg.caption or ""})
            elif msg.document:
                wz["payload"].append({"type": "document", "file_id": msg.document.file_id, "caption": msg.caption or ""})
            else:
                t = (msg.text or "").strip()
                if t:
                    wz["payload"].append({"type": "text", "text": t})
            await msg.reply_text("✅ Added. Send more or DONE.")
            return

        if step == "buttons":
            raw = (msg.text or "").strip()
            if raw.upper() == "SKIP":
                wz["buttons"] = []
            else:
                wz["buttons"] = parse_buttons_text(raw)

            rid = add_auto_reply(wz["pattern"], int(wz["priority"]), wz["payload"], wz["buttons"])
            context.user_data.pop("ar_wz", None)
            await msg.reply_text(f"✅ Auto reply saved. Rule ID: `{rid}`", parse_mode="Markdown", reply_markup=admin_ar_menu())
            return

    # --- Admin wizard: KB ---
    if require_admin(u.id) and context.user_data.get("kb_wz"):
        wz = context.user_data["kb_wz"]
        step = wz.get("step")

        if step == "title":
            title = (msg.text or "").strip()
            if not title:
                await msg.reply_text("Send a valid title.")
                return
            wz["title"] = title
            wz["step"] = "content"
            await msg.reply_text("Now send article content (text).", parse_mode="Markdown")
            return

        if step == "content":
            content = (msg.text or "").strip()
            if not content:
                await msg.reply_text("Send content text.")
                return
            wz["content"] = content
            wz["step"] = "tags"
            await msg.reply_text("Send tags (comma separated) or type SKIP.\nExample: `otp,login,error`", parse_mode="Markdown")
            return

        if step == "tags":
            tags = (msg.text or "").strip()
            if tags.upper() == "SKIP":
                tags = ""
            aid = add_kb_article(wz["title"], wz["content"], tags)
            context.user_data.pop("kb_wz", None)
            await msg.reply_text(f"✅ KB article saved. ID: `{aid}`", parse_mode="Markdown", reply_markup=admin_kb_menu())
            return

    # --- Admin wizard: Broadcast ---
    if require_admin(u.id) and context.user_data.get("bc_wz"):
        wz = context.user_data["bc_wz"]
        step = wz.get("step")

        if step == "collect":
            if (msg.text or "").strip().upper() == "DONE":
                wz["step"] = "buttons"
                await msg.reply_text("Send buttons (optional) or type SKIP.", parse_mode="Markdown")
                return

            if msg.photo:
                wz["payload"].append({"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption or ""})
            elif msg.video:
                wz["payload"].append({"type": "video", "file_id": msg.video.file_id, "caption": msg.caption or ""})
            elif msg.document:
                wz["payload"].append({"type": "document", "file_id": msg.document.file_id, "caption": msg.caption or ""})
            else:
                t = (msg.text or "").strip()
                if t:
                    wz["payload"].append({"type": "text", "text": t})
            await msg.reply_text("✅ Added. Send more or DONE.")
            return

        if step == "buttons":
            raw = (msg.text or "").strip()
            if raw.upper() == "SKIP":
                wz["buttons"] = []
            else:
                wz["buttons"] = parse_buttons_text(raw)

            # Save broadcast
            bid = create_broadcast(u.id, wz["payload"], wz["buttons"])
            context.user_data.pop("bc_wz", None)

            # Send to all users
            users = list_all_users()
            sent = 0
            failed = 0
            for uid in users:
                try:
                    await send_payload(context.bot, uid, wz["payload"], wz["buttons"])
                    log_broadcast(bid, uid, True, "")
                    sent += 1
                except Exception as e:
                    log_broadcast(bid, uid, False, str(e))
                    failed += 1

            await msg.reply_text(f"📢 Broadcast done ✅\nSent: {sent}\nFailed: {failed}", reply_markup=admin_bc_menu())
            return

    # --- Admin chat mode ---
    chat_tid = context.user_data.get("chat_ticket_id")
    if require_admin(u.id) and chat_tid:
        # Send admin message to ticket user
        t = get_ticket(int(chat_tid))
        if not t:
            context.user_data.pop("chat_ticket_id", None)
            await msg.reply_text("❌ Ticket missing. Chat mode ended.")
            return
        target_user = int(t["user_id"])

        # Detect type
        if msg.photo:
            file_id = msg.photo[-1].file_id
            await context.bot.send_photo(target_user, file_id, caption=msg.caption or "")
            add_ticket_message(int(chat_tid), True, u.id, "photo", file_id=file_id, caption=msg.caption or "")
        elif msg.video:
            file_id = msg.video.file_id
            await context.bot.send_video(target_user, file_id, caption=msg.caption or "")
            add_ticket_message(int(chat_tid), True, u.id, "video", file_id=file_id, caption=msg.caption or "")
        elif msg.document:
            file_id = msg.document.file_id
            await context.bot.send_document(target_user, file_id, caption=msg.caption or "")
            add_ticket_message(int(chat_tid), True, u.id, "document", file_id=file_id, caption=msg.caption or "")
        else:
            text = (msg.text or "").strip()
            if text:
                await context.bot.send_message(target_user, f"🛠 Support: {text}")
                add_ticket_message(int(chat_tid), True, u.id, "text", text=text)
        await msg.reply_text("✅ Sent to user. (/endchat to stop)")
        return

    # --- Mode-based actions (admin toggles/deletes etc.) ---
    mode = context.user_data.get("mode")

    if require_admin(u.id) and mode == "ar_toggle":
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("Send numeric Rule ID.")
            return
        v = toggle_auto_reply(int(raw))
        context.user_data["mode"] = None
        if v is None:
            await msg.reply_text("❌ Rule not found.", reply_markup=admin_ar_menu())
        else:
            await msg.reply_text(f"✅ Rule updated. Enabled = {bool(v)}", reply_markup=admin_ar_menu())
        return

    if require_admin(u.id) and mode == "ar_delete":
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("Send numeric Rule ID.")
            return
        ok = delete_auto_reply(int(raw))
        context.user_data["mode"] = None
        await msg.reply_text("✅ Deleted." if ok else "❌ Not found.", reply_markup=admin_ar_menu())
        return

    if require_admin(u.id) and mode == "kb_toggle":
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("Send numeric Article ID.")
            return
        v = toggle_kb_article(int(raw))
        context.user_data["mode"] = None
        if v is None:
            await msg.reply_text("❌ Article not found.", reply_markup=admin_kb_menu())
        else:
            await msg.reply_text(f"✅ Article updated. Enabled = {bool(v)}", reply_markup=admin_kb_menu())
        return

    if require_admin(u.id) and mode == "kb_delete":
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("Send numeric Article ID.")
            return
        ok = delete_kb_article(int(raw))
        context.user_data["mode"] = None
        await msg.reply_text("✅ Deleted." if ok else "❌ Not found.", reply_markup=admin_kb_menu())
        return

    if require_admin(u.id) and mode == "team_add":
        if not require_owner(u.id):
            context.user_data["mode"] = None
            await msg.reply_text("❌ Only owner can add roles.", reply_markup=admin_team_menu())
            return
        raw = (msg.text or "").strip().split()
        if len(raw) != 2 or not raw[0].isdigit() or raw[1] not in ("admin", "agent"):
            await msg.reply_text("Format: `<user_id> <admin|agent>`")
            return
        uid = int(raw[0]); role = raw[1]
        con = _conn(); cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO admins(user_id, role, added_at) VALUES(?,?,?)", (uid, role, int(time.time())))
        con.commit(); cur.close(); con.close()
        context.user_data["mode"] = None
        await msg.reply_text(f"✅ Added `{uid}` as *{role}*.", parse_mode="Markdown", reply_markup=admin_team_menu())
        return

    # --- USER KB search ---
    if not require_admin(u.id) and mode == "kb_search":
        q = (msg.text or "").strip()
        context.user_data["mode"] = None
        rows = search_kb(q, 10)
        if not rows:
            await msg.reply_text("❌ No results. Try another keyword.", reply_markup=user_menu())
            return
        kb = []
        for r in rows:
            kb.append([InlineKeyboardButton(r["title"], callback_data=f"U_KB_OPEN_{r['id']}")])
        kb.append([InlineKeyboardButton("🏠 Home", callback_data="U_BACK_HOME")])
        await msg.reply_text("📚 Results:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # --- USER new ticket (await text/media) ---
    if not require_admin(u.id) and mode == "new_ticket_await_text":
        pr = context.user_data.get("new_ticket_priority", "medium")

        # Accept text/photo/video/document as initial ticket message
        tid = None
        if msg.photo:
            tid = create_ticket(u, "[photo]", pr)
            add_ticket_message(tid, False, u.id, "photo", file_id=msg.photo[-1].file_id, caption=msg.caption or "")
        elif msg.video:
            tid = create_ticket(u, "[video]", pr)
            add_ticket_message(tid, False, u.id, "video", file_id=msg.video.file_id, caption=msg.caption or "")
        elif msg.document:
            tid = create_ticket(u, "[document]", pr)
            add_ticket_message(tid, False, u.id, "document", file_id=msg.document.file_id, caption=msg.caption or "")
        else:
            text = (msg.text or "").strip()
            if not text:
                await msg.reply_text("Send some details (text/photo/video/doc).")
                return
            tid = create_ticket(u, text, pr)

        set_user_current_ticket(u.id, tid)
        context.user_data["mode"] = None

        await msg.reply_text(f"✅ Ticket created! Your Ticket ID: *#{tid}*\nWe will reply soon.", parse_mode="Markdown", reply_markup=user_menu())

        # Notify admins
        con = _conn(); cur = con.cursor()
        cur.execute("SELECT user_id, role FROM admins")
        admins = cur.fetchall()
        cur.close(); con.close()

        note = (
            f"🆕 *New Ticket #{tid}*\n"
            f"From: `{u.id}` @{u.username or '-'} ({u.first_name or '-'})\n"
            f"Priority: *{pr}*\n\n"
            f"Open: /admin → Tickets\n"
        )
        for a in admins:
            try:
                await context.bot.send_message(int(a["user_id"]), note, parse_mode="Markdown")
            except Exception:
                pass
        return

    # --- USER message routing: if active ticket, append to ticket ---
    if not require_admin(u.id):
        active_tid = get_user_current_ticket(u.id)
        if active_tid:
            t = get_ticket(int(active_tid))
            if t and t["status"] in ("open", "pending"):
                if msg.photo:
                    add_ticket_message(active_tid, False, u.id, "photo", file_id=msg.photo[-1].file_id, caption=msg.caption or "")
                elif msg.video:
                    add_ticket_message(active_tid, False, u.id, "video", file_id=msg.video.file_id, caption=msg.caption or "")
                elif msg.document:
                    add_ticket_message(active_tid, False, u.id, "document", file_id=msg.document.file_id, caption=msg.caption or "")
                else:
                    text = (msg.text or "").strip()
                    if text:
                        add_ticket_message(active_tid, False, u.id, "text", text=text)
                await msg.reply_text("✅ Added to your ticket. Support will respond soon.", reply_markup=user_menu())

                # Notify assigned admin (if any) else all admins
                t = get_ticket(int(active_tid))
                target_admins = []
                if t and t["assigned_to"]:
                    target_admins = [int(t["assigned_to"])]
                else:
                    con = _conn(); cur = con.cursor()
                    cur.execute("SELECT user_id FROM admins")
                    target_admins = [int(x["user_id"]) for x in cur.fetchall()]
                    cur.close(); con.close()
                for aid in target_admins:
                    try:
                        await context.bot.send_message(aid, f"👤 Update on Ticket #{active_tid} from `{u.id}`", parse_mode="Markdown")
                    except Exception:
                        pass
                return

        # If no active ticket: try auto reply
        if msg.text:
            rule = find_auto_reply(msg.text)
            if rule:
                await send_payload(context.bot, u.id, rule["payload"], rule["buttons"], fallback_menu=user_menu())
                return

        await msg.reply_text("Menu se choose karo 👇", reply_markup=user_menu())
        return

    # Default for admin (not in any mode)
    if require_admin(u.id):
        await msg.reply_text("👑 Admin Panel:", reply_markup=admin_main_menu())

# ---------- COMMANDS ----------

async def endchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not require_admin(u.id):
        return
    if context.user_data.get("chat_ticket_id"):
        context.user_data.pop("chat_ticket_id", None)
        await update.effective_message.reply_text("✅ Chat mode ended.", reply_markup=admin_main_menu())
    else:
        await update.effective_message.reply_text("Chat mode is not active.")

async def ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u)
    if require_admin(u.id):
        await update.effective_message.reply_text("Admins manage tickets via /admin → Tickets.")
        return
    parts = (update.effective_message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.effective_message.reply_text("Usage: /ticket <id>\nExample: /ticket 12")
        return
    tid = int(parts[1])
    t = get_ticket(tid)
    if not t or t["user_id"] != u.id:
        await update.effective_message.reply_text("❌ Ticket not found.")
        return
    set_user_current_ticket(u.id, tid)
    await update.effective_message.reply_text(f"✅ Active ticket set to #{tid}. Now send messages to add.", reply_markup=user_menu())

async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not require_owner(u.id):
        await update.effective_message.reply_text("❌ Only owner can set roles.")
        return
    parts = (update.effective_message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("owner", "admin", "agent"):
        await update.effective_message.reply_text("Usage: /setrole <user_id> <owner|admin|agent>")
        return
    uid = int(parts[1])
    role = parts[2]
    con = _conn()
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO admins(user_id, role, added_at) VALUES(?,?,?)", (uid, role, int(time.time())))
    con.commit()
    cur.close()
    con.close()
    await update.effective_message.reply_text(f"✅ Role updated: `{uid}` -> *{role}*", parse_mode="Markdown")

# --------------------- BUILD ---------------------

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("endchat", endchat))
    app.add_handler(CommandHandler("ticket", ticket_cmd))
    app.add_handler(CommandHandler("setrole", setrole))

    app.add_handler(CallbackQueryHandler(callbacks))

    # Router catches text + media
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND,
        router
    ))

    return app

# --------------------- RUN ---------------------

if __name__ == "__main__":
    init_db()
    app = build_app()

    app_url = get_app_url()
    port = int(os.getenv("PORT", "8080"))

    if app_url:
        if not app_url.startswith("http"):
            app_url = "https://" + app_url
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{app_url}/{BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
