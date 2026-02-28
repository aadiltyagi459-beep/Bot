import os
import re
import time
import psycopg2
from psycopg2.extras import RealDictCursor

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_URL = (os.getenv("APP_URL", "") or os.getenv("RENDER_EXTERNAL_URL", "")).strip()  # Render auto URL fallback
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()    # e.g. "123,456"
ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in env")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing in env (add Railway Postgres)")

FAQ_TEXT = (
    "🧾 *FAQ*\n\n"
    "1) *Support timing:* 10 AM – 8 PM\n"
    "2) *Response time:* 1–6 hours\n"
    "3) *Ticket banane ke liye:* Create Ticket → apna message bhejo\n\n"
    "_Need help? Create a ticket._"
)

MENU_TEXT = "👋 *Support Center*\n\nChoose an option:"

def db():
    # Railway provides postgres://... which psycopg2 supports
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        username TEXT,
        first_name TEXT,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at BIGINT NOT NULL,
        closed_at BIGINT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS replies (
        id SERIAL PRIMARY KEY,
        ticket_id INT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
        admin_id BIGINT NOT NULL,
        text TEXT NOT NULL,
        created_at BIGINT NOT NULL
    );
    """)
    con.commit()
    cur.close()
    con.close()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📌 FAQ", callback_data="FAQ")],
        [InlineKeyboardButton("🎫 Create Ticket", callback_data="NEW_TICKET")],
        [InlineKeyboardButton("📂 My Tickets", callback_data="MY_TICKETS")],
        [InlineKeyboardButton("✅ Close Ticket", callback_data="CLOSE_TICKET")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = None
    await update.effective_message.reply_text(
        MENU_TEXT,
        parse_mode="Markdown",
        reply_markup=main_menu_kb()
    )

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "FAQ":
        context.user_data["mode"] = None
        await q.message.reply_text(FAQ_TEXT, parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == "NEW_TICKET":
        context.user_data["mode"] = "await_ticket_text"
        await q.message.reply_text(
            "✍️ Apni problem details bhejo.\n\n"
            "Example: *Order ID*, issue, screenshot (optional).",
            parse_mode="Markdown"
        )

    elif data == "MY_TICKETS":
        context.user_data["mode"] = None
        user_id = q.from_user.id
        con = db()
        cur = con.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, status, created_at
            FROM tickets
            WHERE user_id=%s
            ORDER BY id DESC
            LIMIT 10
        """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        con.close()

        if not rows:
            await q.message.reply_text("📭 Aapke koi tickets nahi hain.", reply_markup=main_menu_kb())
            return

        lines = ["📂 *Your last tickets:*"]
        for r in rows:
            t = time.strftime("%d-%b %H:%M", time.localtime(r["created_at"]))
            lines.append(f"• Ticket #{r['id']} — *{r['status']}* — {t}")
        await q.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_kb())

    elif data == "CLOSE_TICKET":
        context.user_data["mode"] = "await_close_ticket_id"
        await q.message.reply_text(
            "✅ Close karne ke liye *Ticket ID* bhejo.\nExample: `12`",
            parse_mode="Markdown"
        )

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    mode = context.user_data.get("mode")

    # User is writing ticket text
    if mode == "await_ticket_text":
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("⚠️ Please message likho.")
            return

        con = db()
        cur = con.cursor()
        now = int(time.time())
        cur.execute("""
            INSERT INTO tickets(user_id, username, first_name, text, status, created_at)
            VALUES(%s,%s,%s,%s,'open',%s)
            RETURNING id
        """, (user.id, user.username, user.first_name, text, now))
        ticket_id = cur.fetchone()[0]
        con.commit()
        cur.close()
        con.close()

        context.user_data["mode"] = None
        await msg.reply_text(
            f"✅ Ticket created! *Ticket #{ticket_id}*\n\n"
            "Admin jaldi reply karega.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )

        # Notify admins
        if ADMIN_IDS:
            note = (
                f"🆕 *New Ticket #{ticket_id}*\n"
                f"From: `{user.id}` @{user.username or '-'} ({user.first_name or '-'})\n\n"
                f"*Message:*\n{text}\n\n"
                f"Reply: `/reply {ticket_id} your message`\n"
                f"Close: `/close {ticket_id}`"
            )
            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(aid, note, parse_mode="Markdown")
                except Exception:
                    pass
        return

    # User is sending ticket id to close
    if mode == "await_close_ticket_id":
        raw = (msg.text or "").strip()
        if not raw.isdigit():
            await msg.reply_text("⚠️ Only ticket number bhejo. Example: `12`", parse_mode="Markdown")
            return

        tid = int(raw)
        con = db()
        cur = con.cursor()
        now = int(time.time())
        cur.execute("""
            UPDATE tickets
            SET status='closed', closed_at=%s
            WHERE id=%s AND user_id=%s AND status='open'
        """, (now, tid, user.id))
        changed = cur.rowcount
        con.commit()
        cur.close()
        con.close()

        context.user_data["mode"] = None
        if changed:
            await msg.reply_text(f"✅ Ticket #{tid} closed.", reply_markup=main_menu_kb())
        else:
            await msg.reply_text("❌ Ticket not found / already closed.", reply_markup=main_menu_kb())
        return

    # Default: show menu hint
    await msg.reply_text("Menu se choose karo 👇", reply_markup=main_menu_kb())

# ---------------- ADMIN COMMANDS ----------------

async def tickets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    con = db()
    cur = con.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, user_id, username, first_name, created_at, text
        FROM tickets
        WHERE status='open'
        ORDER BY id DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    cur.close()
    con.close()

    if not rows:
        await update.effective_message.reply_text("✅ No open tickets.")
        return

    lines = ["🎫 *Open Tickets:*"]
    for r in rows:
        t = time.strftime("%d-%b %H:%M", time.localtime(r["created_at"]))
        uname = f"@{r['username']}" if r["username"] else "-"
        preview = (r["text"][:80] + "…") if len(r["text"]) > 80 else r["text"]
        lines.append(f"\n*#{r['id']}* — `{r['user_id']}` {uname} ({r['first_name'] or '-'}) — {t}\n_{preview}_")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        return

    text = update.effective_message.text or ""
    m = re.match(r"^/reply\s+(\d+)\s+(.+)$", text, re.S)
    if not m:
        await update.effective_message.reply_text("Usage: /reply <ticket_id> <message>")
        return

    tid = int(m.group(1))
    reply_text = m.group(2).strip()
    if not reply_text:
        await update.effective_message.reply_text("⚠️ Reply message empty.")
        return

    con = db()
    cur = con.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT user_id, status FROM tickets WHERE id=%s", (tid,))
    row = cur.fetchone()
    if not row:
        cur.close(); con.close()
        await update.effective_message.reply_text("❌ Ticket not found.")
        return

    # Save reply
    now = int(time.time())
    cur2 = con.cursor()
    cur2.execute("""
        INSERT INTO replies(ticket_id, admin_id, text, created_at)
        VALUES(%s,%s,%s,%s)
    """, (tid, admin_id, reply_text, now))
    con.commit()
    cur2.close()
    cur.close()
    con.close()

    # Send to user
    try:
        await context.bot.send_message(row["user_id"], f"💬 *Support Reply (Ticket #{tid})*\n\n{reply_text}", parse_mode="Markdown")
    except Exception:
        pass

    await update.effective_message.reply_text(f"✅ Replied to Ticket #{tid}.")

async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if not is_admin(admin_id):
        return

    parts = (update.effective_message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.effective_message.reply_text("Usage: /close <ticket_id>")
        return
    tid = int(parts[1])

    con = db()
    cur = con.cursor()
    now = int(time.time())
    cur.execute("""
        UPDATE tickets SET status='closed', closed_at=%s
        WHERE id=%s AND status='open'
    """, (now, tid))
    changed = cur.rowcount
    con.commit()
    cur.close()
    con.close()

    if changed:
        await update.effective_message.reply_text(f"✅ Ticket #{tid} closed.")
    else:
        await update.effective_message.reply_text("❌ Ticket not found / already closed.")

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))

    # Admin commands
    app.add_handler(CommandHandler("tickets", tickets_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("reply", reply_cmd))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    return app

if __name__ == "__main__":
    init_db()
    app = build_app()

    # If APP_URL / RENDER_EXTERNAL_URL is available, run in webhook mode (best for Render Web Service).
    # Otherwise fall back to polling (useful for local testing).
    if APP_URL:
        port = int(os.getenv("PORT", "10000"))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=BOT_TOKEN,
            webhook_url=f"{APP_URL}/{BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
