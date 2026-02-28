import random, string, smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ================= CONFIG =================
BOT_TOKEN = "8590424969:AAG3VgeNponSZTXo3RpnbZg3Z0iBJrJwVUY"

BOT_USERNAME = "gmaillfarmmer_bot"  # without @

EMAIL_SENDER = "tyagizaid469@gmail.com"
EMAIL_PASSWORD = "jfis majz rqmv hyaa"
EMAIL_RECEIVER = "aadiltyagi459@gmail.com"
# =========================================

users = {}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Register a new account", "📒 My accounts"],
        ["💰 Balance", "👥 My referrals"],
        ["⚙️ Settings", "💬 Help"]
    ],
    resize_keyboard=True
)

BALANCE_MENU = ReplyKeyboardMarkup(
    [["💳 Payout", "🧾 Balance history"], ["🔙 Back"]],
    resize_keyboard=True
)

def generate_data():
    name = random.choice(["Aman", "Zaid", "Rohan"]) + " " + random.choice(["Khan", "Tyagi"])
    email = name.replace(" ", "").lower() + str(random.randint(100,999)) + "@gmail.com"
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    return name, email, password


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    uid = user.id

    if uid not in users:
        users[uid] = {
            "count": 0,
            "balance": 0,
            "history": [],
            "awaiting_payout": False,
            "referrer": None,
            "referrals": [],
            "joined": datetime.now()
        }

        # REFERRAL HANDLING
        if context.args:
            ref_id = int(context.args[0])
            if ref_id in users and uid not in users[ref_id]["referrals"]:
                users[uid]["referrer"] = ref_id
                users[ref_id]["referrals"].append(uid)

    await update.message.reply_text("Welcome 👋", reply_markup=MAIN_MENU)


async def register_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    users[uid]["count"] += 1
    users[uid]["balance"] += 10
    users[uid]["history"].append("+₹10 Data Generated")

    name, email, password = generate_data()

    await update.message.reply_text(
        f"""✅ GENERATED DATA

👤 Name:
`{name}`

📧 Email:
`{email}`

🔐 Password:
`{password}`

💰 Balance: ₹{users[uid]['balance']}
""",
        parse_mode="Markdown"
    )


async def referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    now = datetime.now()
    last30 = now - timedelta(days=30)

    total_refs = len(users[uid]["referrals"])
    joined_30 = [r for r in users[uid]["referrals"] if users[r]["joined"] >= last30]
    active_30 = [r for r in joined_30 if users[r]["count"] > 0]

    referral_link = f"https://t.me/{BOT_USERNAME}?start={uid}"

    text = f"""
👥 Total referrals: {total_refs}
💪 Referrals who registered at least one Gmail: {len([r for r in users[uid]['referrals'] if users[r]['count']>0])}

────────────
🆕 New referrals (30 days): {len(joined_30)}
🆕 Active referrals (30 days): {len(active_30)}

────────────
📅 Referrals visited (30 days): {len(joined_30)}
📊 Referrals registered Gmail (30 days): {len(active_30)}

────────────
💰 Your profit last 30 days: ₹{len(active_30)*10}

────────────
🔗 Your referral link:
{referral_link}
"""
    await update.message.reply_text(text)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(
        f"💰 Balance: ₹{users[uid]['balance']}",
        reply_markup=BALANCE_MENU
    )


async def payout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users[update.message.from_user.id]["awaiting_payout"] = True
    await update.message.reply_text("PLEASE ENTER YOUR UPI ID OR QR CODE")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if users[uid]["awaiting_payout"]:
        users[uid]["awaiting_payout"] = False
        await update.message.reply_text("✅ Payout request sent!")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^➕ Register a new account$"), register_account))
    app.add_handler(MessageHandler(filters.Regex("^👥 My referrals$"), referrals))
    app.add_handler(MessageHandler(filters.Regex("^💰 Balance$"), balance))
    app.add_handler(MessageHandler(filters.Regex("^💳 Payout$"), payout))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling()

if __name__ == "__main__":
    main()