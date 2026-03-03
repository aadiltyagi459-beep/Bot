import os
import json
import time
import sqlite3
import openai
from telegram import *
from telegram.ext import *
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import io

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x}
FORCE_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL")
GDRIVE_FILE_ID = os.getenv("GDRIVE_FILE_ID")
SERVICE_JSON = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON")

openai.api_key = OPENAI_API_KEY

DB = "bot.db"

# ================= DATABASE =================

def db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    cur = c.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS auto (trigger TEXT PRIMARY KEY, reply TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS ai (enabled INTEGER)")
    cur.execute("INSERT OR IGNORE INTO ai VALUES (1)")
    c.commit()

# ================= GOOGLE DRIVE =================

def drive_service():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_JSON),
        scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def backup_drive():
    service = drive_service()
    media = MediaFileUpload(DB, mimetype='application/octet-stream')
    service.files().update(fileId=GDRIVE_FILE_ID, media_body=media).execute()

# ================= AI =================

def ai_reply(text):
    if not OPENAI_API_KEY:
        return "AI not configured."
    try:
        res = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system","content":"You are professional customer support agent. Reply short and helpful."},
                {"role":"user","content":text}
            ],
            max_tokens=150
        )
        return res.choices[0].message.content
    except:
        return "AI error."

# ================= FORCE JOIN =================

async def check_join(update, context):
    if not FORCE_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_CHANNEL, update.effective_user.id)
        if member.status in ["member","administrator","creator"]:
            return True
    except:
        pass
    kb = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/gmail_earning")]]
    await update.effective_message.reply_text("Please join channel first.", reply_markup=InlineKeyboardMarkup(kb))
    return False

# ================= MENUS =================

def user_menu():
    return ReplyKeyboardMarkup(
        [["💬 Contact Support"]],
        resize_keyboard=True
    )

def admin_menu():
    return ReplyKeyboardMarkup(
        [["💬 Live Chat","🤖 Auto Reply"],
         ["🧠 AI Toggle","☁️ Backup"]],
        resize_keyboard=True
    )

# ================= HANDLERS =================

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("Admin Panel", reply_markup=admin_menu())
    else:
        await update.message.reply_text("Welcome Support", reply_markup=user_menu())

async def text_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not await check_join(update, context):
        return

    text = update.message.text
    uid = update.effective_user.id

    # ADMIN COMMANDS
    if uid in ADMIN_IDS:
        if text.startswith("/set "):
            try:
                t,r = text.replace("/set ","").split("|",1)
                c=db();cur=c.cursor()
                cur.execute("INSERT OR REPLACE INTO auto VALUES (?,?)",(t.strip(),r.strip()))
                c.commit()
                await update.message.reply_text("Saved.")
            except:
                await update.message.reply_text("Format: /set trigger | reply")
            return

        if text=="🧠 AI Toggle":
            c=db();cur=c.cursor()
            cur.execute("SELECT enabled FROM ai")
            val=cur.fetchone()[0]
            new=0 if val else 1
            cur.execute("UPDATE ai SET enabled=?",(new,))
            c.commit()
            await update.message.reply_text(f"AI {'ON' if new else 'OFF'}")
            return

        if text=="☁️ Backup":
            backup_drive()
            await update.message.reply_text("Backup Done.")
            return

    # EXACT AUTO REPLY
    c=db();cur=c.cursor()
    cur.execute("SELECT reply FROM auto WHERE trigger=?",(text,))
    row=cur.fetchone()
    if row:
        await update.message.reply_text(row["reply"])
        return

    # AI MODE
    cur.execute("SELECT enabled FROM ai")
    ai_on=cur.fetchone()[0]
    if ai_on:
        reply=ai_reply(text)
        await update.message.reply_text(reply)
        return

    # LIVE SUPPORT
    for admin in ADMIN_IDS:
        await context.bot.send_message(admin,f"User {uid}: {text}")

    await update.message.reply_text("Sent to support. Wait.")

# ================= RUN =================

if __name__=="__main__":
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    APP_URL=os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    PORT=int(os.getenv("PORT",8080))

    if APP_URL:
        if not APP_URL.startswith("http"):
            APP_URL="https://"+APP_URL
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{APP_URL}/{BOT_TOKEN}",
        )
    else:
        app.run_polling()
