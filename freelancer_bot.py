import logging
import sqlite3
import asyncio
import os
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler, ContextTypes

# ========================
# 🛠️ 1. CONFIG (BAS YAHI BADALNA HAI)
# ========================
TOKEN = "8933448449:AAFIAjkG8javW6QkRI0jaOQlVkEP2loAWa4"          # @BotFather se lo
ADMIN_ID = 7572500106                   # Apna Telegram ID (Integer)
MERCHANT_UPI = "admin@upi"             # Apna real UPI ID
BOT_USERNAME = "BidforWorkBot"       # Bin @ ke, jaise "BidforWorkBot"

LEAD_FEE = 10
INITIAL_BALANCE = 100
PRICE_DROP_INTERVAL = 180              # 3 minutes
BOOKING_EXPIRE_HOURS = 24
FREE_LEADS_LIMIT = 5
REFERRAL_BONUS = 2

# ========================
# 🗃️ 2. DATABASE SETUP
# ========================
conn = sqlite3.connect("bot_data.db", check_same_thread=False)
cursor = conn.cursor()

# Users Table
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    wallet_balance INTEGER DEFAULT 100,
    is_banned INTEGER DEFAULT 0,
    total_earnings INTEGER DEFAULT 0,
    joined_at TEXT,
    free_leads_remaining INTEGER DEFAULT 5,
    referred_by INTEGER,
    referral_count INTEGER DEFAULT 0,
    referral_earnings INTEGER DEFAULT 0
)
""")

# Listings Table
cursor.execute("""
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER,
    category TEXT,
    title TEXT,
    description TEXT,
    max_price INTEGER,
    min_price INTEGER,
    current_price INTEGER,
    delivery_time TEXT,
    photo_file_id TEXT,
    message_id INTEGER,
    chat_id INTEGER,
    status TEXT DEFAULT 'active',
    views INTEGER DEFAULT 0,
    created_at TEXT,
    next_drop_time TEXT,
    drop_step INTEGER DEFAULT 50,
    booked_at TEXT
)
""")

# Leads Table
cursor.execute("""
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER,
    buyer_id INTEGER,
    seller_id INTEGER,
    buyer_phone TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    completed_at TEXT
)
""")

# Ratings Table
cursor.execute("""
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER,
    to_user_id INTEGER,
    lead_id INTEGER,
    rating INTEGER,
    comment TEXT,
    created_at TEXT
)
""")

# Backward compatibility (if older DB)
try:
    cursor.execute("ALTER TABLE users ADD COLUMN free_leads_remaining INTEGER DEFAULT 5")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE users ADD COLUMN referral_earnings INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
try:
    cursor.execute("ALTER TABLE listings ADD COLUMN booked_at TEXT")
except sqlite3.OperationalError:
    pass

conn.commit()

# ========================
# 🧠 3. HELPER FUNCTIONS
# ========================
def get_user(telegram_id):
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    user = cursor.fetchone()
    if not user:
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO users (telegram_id, joined_at, free_leads_remaining) 
            VALUES (?, ?, ?)
        """, (telegram_id, now, FREE_LEADS_LIMIT))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        user = cursor.fetchone()
    return user

def format_price(price):
    return f"₹{price:,}"

def get_listing(listing_id):
    cursor.execute("SELECT * FROM listings WHERE id = ?", (listing_id,))
    return cursor.fetchone()

def get_seller_stats(seller_id):
    cursor.execute("SELECT COUNT(*) FROM leads WHERE seller_id = ? AND status = 'completed'", (seller_id,))
    return cursor.fetchone()[0]

def is_admin(user_id):
    return user_id == ADMIN_ID

def rebuild_listing_caption(listing_id):
    cursor.execute("""
        SELECT title, description, current_price, delivery_time, seller_id, status, max_price, min_price 
        FROM listings WHERE id = ?
    """, (listing_id,))
    data = cursor.fetchone()
    if not data:
        return "Listing not found."
    
    title, desc, price, delivery, seller_id, status, max_p, min_p = data
    completed = get_seller_stats(seller_id)
    
    status_emoji = "🟢" if status == "active" else "🔴"
    caption = (
        f"🔥 *{title}*\n"
        f"📂 {status_emoji} {status.upper()}\n"
        f"📝 {desc[:200]}...\n\n"
        f"💰 *Live Price:* {format_price(price)}\n"
        f"📉 Range: {format_price(max_p)} → {format_price(min_p)}\n"
        f"📦 Delivery: {delivery}\n"
        f"🆔 ID: {listing_id}\n\n"
        f"👤 Seller Jobs Done: {completed}"
    )
    return caption

# ========================
# ⏰ 4. BACKGROUND TASKS
# ========================
async def check_price_drops(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now().isoformat()
    
    # Drop prices for active listings
    cursor.execute("""
        SELECT id, current_price, min_price, drop_step, message_id, chat_id 
        FROM listings WHERE status = 'active' AND next_drop_time <= ?
    """, (now,))
    listings = cursor.fetchall()
    
    for listing in listings:
        listing_id, current_price, min_price, drop_step, msg_id, chat_id = listing
        new_price = current_price - drop_step
        
        if new_price <= min_price:
            new_price = min_price
            cursor.execute("UPDATE listings SET status = 'expired', current_price = ? WHERE id = ?", (new_price, listing_id))
            conn.commit()
            await context.bot.send_message(chat_id=chat_id, text=f"⏰ Auction ended! Final: {format_price(new_price)} for #{listing_id}")
        else:
            next_drop = (datetime.now() + timedelta(seconds=PRICE_DROP_INTERVAL)).isoformat()
            cursor.execute("UPDATE listings SET current_price = ?, next_drop_time = ? WHERE id = ?", (new_price, next_drop, listing_id))
            conn.commit()
            
            try:
                new_caption = rebuild_listing_caption(listing_id)
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=msg_id,
                    caption=new_caption,
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Edit error: {e}")

    # Auto-release expired bookings (buyer didn't respond)
    expire_time = (datetime.now() - timedelta(hours=BOOKING_EXPIRE_HOURS)).isoformat()
    cursor.execute("""
        SELECT id, seller_id, message_id, chat_id FROM listings 
        WHERE status = 'booked' AND booked_at <= ?
    """, (expire_time,))
    expired_bookings = cursor.fetchall()
    
    for exp in expired_bookings:
        listing_id, seller_id, msg_id, chat_id = exp
        cursor.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE telegram_id = ?", (LEAD_FEE, seller_id))
        cursor.execute("UPDATE listings SET status = 'active', booked_at = NULL WHERE id = ?", (listing_id,))
        conn.commit()
        
        await context.bot.send_message(chat_id=seller_id, text=f"🔄 Lead expired! Buyer didn't respond. ₹{LEAD_FEE} refunded. Listing #{listing_id} active again.")

# ========================
# 👋 5. BOT COMMANDS
# ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user[4] == 1:
        await update.message.reply_text("🚫 You are banned.")
        return
    
    # Referral logic
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload.split("_")[1])
                if referrer_id != user[1] and user[8] is None:
                    ref_user = get_user(referrer_id)
                    if ref_user[4] != 1:
                        cursor.execute("""
                            UPDATE users SET 
                            wallet_balance = wallet_balance + ?, 
                            referral_count = referral_count + 1,
                            referral_earnings = referral_earnings + ?
                            WHERE telegram_id = ?
                        """, (REFERRAL_BONUS, REFERRAL_BONUS, referrer_id))
                        cursor.execute("UPDATE users SET referred_by = ? WHERE telegram_id = ?", (referrer_id, user[1]))
                        conn.commit()
                        
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 *New Referral!* Someone joined using your link.\n💰 +₹{REFERRAL_BONUS} added to your wallet!",
                            parse_mode="Markdown"
                        )
                        await update.message.reply_text(f"🎉 Welcome! You were referred by a friend. They got ₹{REFERRAL_BONUS} bonus!")
            except Exception as e:
                print(f"Referral error: {e}")
    
    user = get_user(update.effective_user.id)
    
    keyboard = [
        [InlineKeyboardButton("🛍️ Sell Service", callback_data="sell")],
        [InlineKeyboardButton("🔍 Browse Deals", callback_data="browse")],
        [InlineKeyboardButton("📦 My Listings", callback_data="my_listings")],
        [InlineKeyboardButton("👛 My Wallet", callback_data="wallet")],
        [InlineKeyboardButton("⭐ Rate Buyer", callback_data="rate_menu")],
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ]
    await update.message.reply_text(
        f"👋 Welcome {update.effective_user.first_name}!\n"
        f"💰 Balance: ₹{user[3]}\n"
        f"🆓 Free Leads Left: {user[7]}\n"
        f"📊 Jobs Done: {get_seller_stats(user[1])}\n\n"
        "🤖 *Dutch Auction Freelancer Bot*\n"
        "Sellers: Price drops every 3 mins till sold.\n"
        "Buyers: Grab the best deal!\n\n"
        "📜 Type /terms for Terms & Conditions\n"
        "🔗 Get your referral link: /referral",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user[4] == 1:
        await update.message.reply_text("🚫 Banned.")
        return
    
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{user[1]}"
    text = (
        f"🔗 *Your Referral Link*\n"
        f"`{ref_link}`\n\n"
        f"📊 *Your Stats*\n"
        f"👥 Total Referrals: {user[9]}\n"
        f"💰 Referral Earnings: ₹{user[10]}\n\n"
        f"🤝 *How it works:*\n"
        f"1. Share this link with your friends.\n"
        f"2. When they join via your link, you get ₹{REFERRAL_BONUS} instantly!\n"
        f"3. No limit on referrals. Keep earning!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    terms_text = (
        "📜 *TERMS AND CONDITIONS* 📜\n\n"
        "1. *Platform Role*: This bot is a lead generation platform. It connects buyers and sellers for freelance services.\n\n"
        "2. *Payments*: All payments for actual services (gigs) are made *directly* between the buyer and seller using UPI, GPay, etc. The bot owner is *NOT responsible* for any payment disputes, service quality, or delivery issues.\n\n"
        "3. *Lead Fee*: Sellers get 5 FREE leads. After that, ₹10 per lead. This fee is deducted from the seller's wallet balance.\n\n"
        "4. *Refund Policy*: If a buyer does not respond to the seller's contact within 24 hours, the ₹10 lead fee is *automatically refunded* to the seller's wallet.\n\n"
        "5. *Rating System*: Only *sellers* can rate buyers after a transaction. This helps build a trustworthy community. Fake or retaliatory ratings may lead to a ban.\n\n"
        "6. *Prohibited Actions*: Spamming, fake listings, fake buying, harassment, or any form of fraud is strictly prohibited. Violators will be *permanently banned* without prior notice.\n\n"
        "7. *Account Responsibility*: Users are responsible for their own account security. Sharing accounts or Telegram IDs is not allowed.\n\n"
        "8. *Changes to Terms*: The admin reserves the right to modify these terms at any time. Continued use of the bot implies acceptance of the latest terms.\n\n"
        "9. *Disputes*: For any disputes, contact the admin directly. The admin's decision will be final.\n\n"
        "✅ By using this bot, you agree to all the above terms."
    )
    await update.message.reply_text(terms_text, parse_mode="Markdown")

async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = (
        "📖 *How to use:*\n\n"
        "1️⃣ *Sell:* Click 'Sell Service', fill details.\n"
        "2️⃣ *Buy:* Click 'Browse Deals', tap 'Hire Now'.\n"
        "3️⃣ *After Hire:* Seller gets your number. Deal directly.\n"
        "4️⃣ *Rate Buyer:* /rate <lead_id> <1-5> (Only Seller)\n"
        "5️⃣ *Cancel Hire:* /cancel_hire <lead_id>\n"
        "6️⃣ *Delete:* /delete <listing_id>\n\n"
        f"🆓 FREE Leads: First {FREE_LEADS_LIMIT} leads are free!\n"
        f"💰 After that: ₹{LEAD_FEE} per lead.\n"
        "🔗 Share your referral link: /referral"
    )
    await query.edit_message_text(text, parse_mode="Markdown")

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update.effective_user.id)
    if user[4] == 1:
        await query.edit_message_text("🚫 Banned.")
        return
    
    await query.edit_message_text(
        f"👛 *Your Wallet*\n"
        f"💰 Balance: ₹{user[3]}\n"
        f"🆓 Free Leads Left: {user[7]}\n"
        f"📊 Total Jobs Done: {user[5]}\n"
        f"🤝 Referral Earnings: ₹{user[10]}\n"
        f"👥 Referrals: {user[9]}\n\n"
        f"➡️ *Add Money:*\n"
        f"Pay to UPI: `{MERCHANT_UPI}`\n"
        f"Type: /addmoney <amount> <txn_id>\n"
        f"(Admin will credit manually)",
        parse_mode="Markdown"
    )

# ========================
# 💰 6. CREDIT SYSTEM
# ========================
async def add_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(f"Usage: /addmoney <amount> <txn_id>\nPay to UPI: `{MERCHANT_UPI}`", parse_mode="Markdown")
        return
    try:
        amount = int(args[0])
        txn = args[1]
        await update.message.reply_text(f"✅ Request sent for ₹{amount} (TXN: {txn}). Admin will verify and credit.")
        await context.bot.send_message(
            chat_id=ADMIN_ID, 
            text=f"💰 *Add Money Request*\nUser: {update.effective_user.id}\nAmount: ₹{amount}\nTXN: {txn}",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("Invalid amount.")

async def admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 Unauthorized.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /admin_add <telegram_id> <amount>")
        return
    try:
        user_id = int(args[0])
        amount = int(args[1])
        if amount < 0:
            await update.message.reply_text("Amount cannot be negative.")
            return
        cursor.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE telegram_id = ?", (amount, user_id))
        conn.commit()
        await update.message.reply_text(f"✅ Added ₹{amount} to user {user_id}.")
        await context.bot.send_message(chat_id=user_id, text=f"💰 ₹{amount} added to your wallet by admin.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# ========================
# 🗑️ 7. DELETE & CANCEL
# ========================
async def delete_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delete <listing_id>")
        return
    try:
        listing_id = int(args[0])
        user = get_user(update.effective_user.id)
        cursor.execute("SELECT seller_id FROM listings WHERE id = ?", (listing_id,))
        res = cursor.fetchone()
        if not res:
            await update.message.reply_text("Listing not found.")
            return
        if res[0] != user[1] and not is_admin(update.effective_user.id):
            await update.message.reply_text("Not your listing.")
            return
        cursor.execute("UPDATE listings SET status = 'expired' WHERE id = ?", (listing_id,))
        conn.commit()
        await update.message.reply_text(f"✅ Listing #{listing_id} deleted.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")

async def cancel_hire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /cancel_hire <lead_id>")
        return
    try:
        lead_id = int(args[0])
        user = get_user(update.effective_user.id)
        cursor.execute("SELECT id, listing_id, seller_id, status FROM leads WHERE id = ?", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            await update.message.reply_text("Lead not found.")
            return
        if lead[2] != user[1] and not is_admin(update.effective_user.id):
            await update.message.reply_text("You don't own this lead.")
            return
        if lead[3] != 'pending':
            await update.message.reply_text("Already completed/cancelled.")
            return
        
        cursor.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE telegram_id = ?", (LEAD_FEE, lead[2]))
        cursor.execute("UPDATE leads SET status = 'cancelled' WHERE id = ?", (lead_id,))
        cursor.execute("UPDATE listings SET status = 'active', booked_at = NULL WHERE id = ?", (lead[1],))
        conn.commit()
        await update.message.reply_text(f"✅ Lead #{lead_id} cancelled. ₹{LEAD_FEE} refunded.")
    except ValueError:
        await update.message.reply_text("Invalid ID.")

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Unauthorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /ban <telegram_id>")
        return
    try:
        uid = int(args[0])
        cursor.execute("UPDATE users SET is_banned = 1 WHERE telegram_id = ?", (uid,))
        conn.commit()
        await update.message.reply_text(f"✅ User {uid} banned.")
    except:
        await update.message.reply_text("Error.")

async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Reset complete! Type /start to begin again.")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("❌ Cancelled. Type /start to go to main menu.")
    elif update.callback_query:
        await update.callback_query.edit_message_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# ========================
# 🛍️ 8. SELL CONVERSATION
# ========================
CATEGORY, TITLE, DESCRIPTION, MAX_PRICE, MIN_PRICE, DELIVERY, PHOTO = range(7)

async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update.effective_user.id)
    if user[4] == 1:
        await query.edit_message_text("🚫 Banned.")
        return ConversationHandler.END
    if user[3] < LEAD_FEE and user[7] == 0:
        await query.edit_message_text(f"❌ Insufficient balance (₹{user[3]}) and no free leads left. Need ₹{LEAD_FEE} to list. /addmoney")
        return ConversationHandler.END
    
    keyboard = [
        [InlineKeyboardButton("💻 Tech/Dev", callback_data="Tech")],
        [InlineKeyboardButton("🎨 Design/Logo", callback_data="Design")],
        [InlineKeyboardButton("✍️ Writing", callback_data="Writing")],
        [InlineKeyboardButton("📱 Social Media", callback_data="Social")],
        [InlineKeyboardButton("📊 Data/Excel", callback_data="Data")]
    ]
    await query.edit_message_text("Select category:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CATEGORY

async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['category'] = query.data
    await query.edit_message_text("Send *Title*:", parse_mode="Markdown")
    return TITLE

async def title_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['title'] = update.message.text
    await update.message.reply_text("Send *Description* (max 300 chars):", parse_mode="Markdown")
    return DESCRIPTION

async def desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(update.message.text) > 300:
        await update.message.reply_text("Too long! Max 300 chars.")
        return DESCRIPTION
    context.user_data['description'] = update.message.text
    await update.message.reply_text("Send *Starting Price* (Max ₹):", parse_mode="Markdown")
    return MAX_PRICE

async def max_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text)
        if val < 10:
            await update.message.reply_text("Min ₹10.")
            return MAX_PRICE
        context.user_data['max_price'] = val
        await update.message.reply_text("Send *Minimum Price* (₹):", parse_mode="Markdown")
        return MIN_PRICE
    except ValueError:
        await update.message.reply_text("Enter a number.")
        return MAX_PRICE

async def min_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text)
        if val < 5 or val >= context.user_data['max_price']:
            await update.message.reply_text("Min must be < Max and at least ₹5.")
            return MIN_PRICE
        context.user_data['min_price'] = val
        await update.message.reply_text("Send *Delivery Time* (e.g., 24 hrs):", parse_mode="Markdown")
        return DELIVERY
    except ValueError:
        await update.message.reply_text("Enter a number.")
        return MIN_PRICE

async def delivery_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['delivery'] = update.message.text
    await update.message.reply_text("Send a *Photo* (Portfolio):", parse_mode="Markdown")
    return PHOTO

async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    photo = update.message.photo[-1]
    file_id = photo.file_id
    
    now = datetime.now().isoformat()
    next_drop = (datetime.now() + timedelta(seconds=PRICE_DROP_INTERVAL)).isoformat()
    max_p = context.user_data['max_price']
    min_p = context.user_data['min_price']
    drop_step = max(10, (max_p - min_p) // 15)
    
    cursor.execute("""
        INSERT INTO listings 
        (seller_id, category, title, description, max_price, min_price, current_price, delivery_time, photo_file_id, created_at, next_drop_time, drop_step)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user[1], context.user_data['category'], context.user_data['title'], context.user_data['description'],
          max_p, min_p, max_p, context.user_data['delivery'], file_id, now, next_drop, drop_step))
    conn.commit()
    listing_id = cursor.lastrowid
    
    caption = rebuild_listing_caption(listing_id)
    msg = await update.message.reply_photo(photo=file_id, caption=caption, parse_mode="Markdown")
    
    cursor.execute("UPDATE listings SET message_id = ?, chat_id = ? WHERE id = ?", (msg.message_id, msg.chat_id, listing_id))
    conn.commit()
    
    await update.message.reply_text(f"✅ Listed! ID: #{listing_id}\nPrice drops every {PRICE_DROP_INTERVAL//60} mins.")
    return ConversationHandler.END

# ========================
# 🔍 9. BROWSE LOGIC
# ========================
async def browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("💻 Tech", callback_data="cat_Tech")],
        [InlineKeyboardButton("🎨 Design", callback_data="cat_Design")],
        [InlineKeyboardButton("✍️ Writing", callback_data="cat_Writing")],
        [InlineKeyboardButton("📱 Social", callback_data="cat_Social")],
        [InlineKeyboardButton("📊 Data", callback_data="cat_Data")],
    ]
    await query.edit_message_text("Select a category:", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_category_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("cat_", "")
    context.user_data['current_category'] = category
    context.user_data['browse_offset'] = 0
    await render_browse_page(update, context, category, 0)

async def render_browse_page(update: Update, context: ContextTypes.DEFAULT_TYPE, category, offset):
    cursor.execute("""
        SELECT id, title, current_price, photo_file_id, message_id, chat_id 
        FROM listings 
        WHERE category = ? AND status = 'active' 
        ORDER BY views DESC LIMIT 10 OFFSET ?
    """, (category, offset))
    listings = cursor.fetchall()
    
    if not listings:
        if offset == 0:
            await update.callback_query.edit_message_text("No active services.")
        else:
            await update.callback_query.answer("End of list!", show_alert=True)
        return
    
    context.user_data['browse_list'] = listings
    context.user_data['browse_index'] = 0
    await show_browse_item(update, context, 0)

async def show_browse_item(update: Update, context: ContextTypes.DEFAULT_TYPE, index):
    listings = context.user_data.get('browse_list', [])
    category = context.user_data.get('current_category', 'Tech')
    offset = context.user_data.get('browse_offset', 0)
    
    if not listings or index >= len(listings):
        new_offset = offset + 10
        context.user_data['browse_offset'] = new_offset
        await render_browse_page(update, context, category, new_offset)
        return
    
    listing_id, title, price, photo_id, msg_id, chat_id = listings[index]
    caption = rebuild_listing_caption(listing_id)
    
    keyboard = []
    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"browse_prev_{index}"))
    if index < len(listings) - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"browse_next_{index}"))
    if nav_row:
        keyboard.append(nav_row)
    
    keyboard.append([InlineKeyboardButton("💰 Hire Now", callback_data=f"hire_{listing_id}")])
    keyboard.append([InlineKeyboardButton("🚩 Report", callback_data=f"report_{listing_id}")])
    
    try:
        await update.callback_query.edit_message_media(
            media=InputMediaPhoto(media=photo_id, caption=caption, parse_mode="Markdown"),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except:
        await update.callback_query.edit_message_text(
            text=f"🖼️ No photo.\n\n{caption}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def browse_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action = data[1]
    current_index = int(data[2])
    
    new_index = current_index + 1 if action == "next" else current_index - 1
    
    listings = context.user_data.get('browse_list', [])
    if new_index < 0:
        await query.answer("First item!", show_alert=True)
        return
    if new_index >= len(listings):
        category = context.user_data.get('current_category', 'Tech')
        new_offset = context.user_data.get('browse_offset', 0) + 10
        context.user_data['browse_offset'] = new_offset
        await render_browse_page(update, context, category, new_offset)
        return
    
    context.user_data['browse_index'] = new_index
    await show_browse_item(update, context, new_index)

# ========================
# 🤝 10. HIRE LOGIC
# ========================
async def hire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update.effective_user.id)
    if user[4] == 1:
        await query.edit_message_text("🚫 Banned.")
        return ConversationHandler.END
    
    listing_id = int(query.data.split("_")[1])
    listing = get_listing(listing_id)
    if not listing or listing[13] != 'active':
        await query.edit_message_text("❌ Not available.")
        return ConversationHandler.END
    
    seller_id = listing[1]
    seller = get_user(seller_id)
    
    if seller[3] < LEAD_FEE and seller[7] == 0:
        await query.edit_message_text("❌ Seller has insufficient balance and no free leads left. They cannot receive leads.")
        return ConversationHandler.END
    
    cursor.execute("UPDATE listings SET status = 'booked', booked_at = ? WHERE id = ?", (datetime.now().isoformat(), listing_id))
    conn.commit()
    
    context.user_data['hire_listing_id'] = listing_id
    await query.edit_message_text("📱 Send your *Phone Number* to contact seller:", parse_mode="Markdown")
    return 1

async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text
    if not re.match(r'^[0-9+\- ]{7,15}$', phone):
        await update.message.reply_text("❌ Invalid number. Try again.")
        return 1
    
    listing_id = context.user_data['hire_listing_id']
    listing = get_listing(listing_id)
    if not listing:
        await update.message.reply_text("Expired.")
        return ConversationHandler.END
    
    buyer = get_user(update.effective_user.id)
    seller_id = listing[1]
    seller = get_user(seller_id)
    
    now = datetime.now().isoformat()
    cursor.execute("INSERT INTO leads (listing_id, buyer_id, seller_id, buyer_phone, created_at) VALUES (?, ?, ?, ?, ?)",
                   (listing_id, buyer[1], seller_id, phone, now))
    conn.commit()
    lead_id = cursor.lastrowid
    
    # Deduct lead fee or use free lead
    free_leads = seller[7]
    if free_leads > 0:
        cursor.execute("UPDATE users SET free_leads_remaining = free_leads_remaining - 1 WHERE telegram_id = ?", (seller_id,))
        lead_cost_msg = "🆓 Free lead used! (5 free leads available initially)"
    else:
        cursor.execute("UPDATE users SET wallet_balance = wallet_balance - ? WHERE telegram_id = ?", (LEAD_FEE, seller_id))
        lead_cost_msg = f"💰 ₹{LEAD_FEE} deducted from wallet."
    
    conn.commit()
    seller = get_user(seller_id)
    
    await context.bot.send_message(
        chat_id=seller_id,
        text=f"🎉 *New Lead!*\nID: #{lead_id}\nService: {listing[3]}\nPhone: {phone}\nBuyer: @{update.effective_user.username or 'No uname'}\n\n{lead_cost_msg}\nFree Leads Remaining: {seller[7]}\n\nAfter work is done, rate buyer: /rate {lead_id} 5",
        parse_mode="Markdown"
    )
    
    await update.message.reply_text(
        f"✅ Lead #{lead_id} sent! Seller will contact you.\n{lead_cost_msg}"
    )
    return ConversationHandler.END

async def report_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listing_id = query.data.split("_")[1]
    user = get_user(update.effective_user.id)
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"🚩 *Report*\nListing: {listing_id}\nUser: {user[1]}",
        parse_mode="Markdown"
    )
    await query.edit_message_text("✅ Reported to admin.")

async def my_listings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(update.effective_user.id)
    cursor.execute("SELECT id, title, current_price, status FROM listings WHERE seller_id = ? ORDER BY id DESC LIMIT 20", (user[1],))
    listings = cursor.fetchall()
    if not listings:
        await query.edit_message_text("No listings.")
        return
    
    text = "📦 *Your Listings:*\n\n"
    for l in listings:
        text += f"#{l[0]} | {l[1][:20]}\n   {format_price(l[2])} | {l[3].upper()}\n"
    text += "\nCommands: /delete <id>"
    await query.edit_message_text(text, parse_mode="Markdown")

# ========================
# ⭐ 11. RATE BUYER (Only Seller)
# ========================
async def rate_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("👤 *Rate Buyer*\nOnly sellers can rate buyers.\n\nType: /rate <lead_id> <1-5>\nExample: `/rate 12 5`", parse_mode="Markdown")

async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /rate <lead_id> <1-5>")
        return
    try:
        lead_id = int(args[0])
        rating = int(args[1])
        if rating < 1 or rating > 5:
            await update.message.reply_text("Rating 1-5 only.")
            return
        
        user = get_user(update.effective_user.id)
        cursor.execute("SELECT buyer_id, seller_id, status FROM leads WHERE id = ?", (lead_id,))
        lead = cursor.fetchone()
        if not lead:
            await update.message.reply_text("Lead not found.")
            return
        
        if lead[1] != user[1]:
            await update.message.reply_text("❌ Only the seller who completed this job can rate the buyer.")
            return
        
        if lead[2] == 'completed':
            await update.message.reply_text("This lead is already rated/completed.")
            return
        
        now = datetime.now().isoformat()
        cursor.execute("INSERT INTO ratings (from_user_id, to_user_id, lead_id, rating, created_at) VALUES (?, ?, ?, ?, ?)",
                       (user[1], lead[0], lead_id, rating, now))
        cursor.execute("UPDATE leads SET status = 'completed', completed_at = ? WHERE id = ?", (now, lead_id))
        cursor.execute("UPDATE users SET total_earnings = total_earnings + 1 WHERE telegram_id = ?", (user[1],))
        conn.commit()
        
        await update.message.reply_text(f"✅ Buyer rated {rating}/5 successfully!")
        
        try:
            await context.bot.send_message(
                chat_id=lead[0],
                text=f"⭐ You were rated {rating}/5 by the seller for lead #{lead_id}."
            )
        except:
            pass
            
    except ValueError:
        await update.message.reply_text("Invalid input.")

# ========================
# 🚀 12. DUMMY WEB SERVER + MAIN
# ========================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        return

def run_web_server(port):
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def main():
    app = Application.builder().token(TOKEN).build()
    
    # Cancel/Reset commands (global safety net)
    app.add_handler(CommandHandler("reset", reset_command))
    app.add_handler(CommandHandler("cancel", cancel))
    
    # Sell Conversation
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(sell_start, pattern="^sell$")],
        states={
            CATEGORY: [CallbackQueryHandler(category_selected, pattern="^(Tech|Design|Writing|Social|Data)$")],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, title_received)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, desc_received)],
            MAX_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, max_price_received)],
            MIN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, min_price_received)],
            DELIVERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, delivery_received)],
            PHOTO: [MessageHandler(filters.PHOTO, photo_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", cancel), CommandHandler("reset", reset_command)]
    )
    
    # Hire Conversation
    hire_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(hire, pattern="^hire_")],
        states={1: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)]},
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", cancel), CommandHandler("reset", reset_command)]
    )
    
    # All commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("terms", terms_command))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("delete", delete_listing))
    app.add_handler(CommandHandler("cancel_hire", cancel_hire))
    app.add_handler(CommandHandler("addmoney", add_money))
    app.add_handler(CommandHandler("admin_add", admin_add))
    app.add_handler(CommandHandler("ban", ban_user))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(wallet, pattern="^wallet$"))
    app.add_handler(CallbackQueryHandler(browse, pattern="^browse$"))
    app.add_handler(CallbackQueryHandler(help_menu, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(rate_menu, pattern="^rate_menu$"))
    app.add_handler(CallbackQueryHandler(my_listings, pattern="^my_listings$"))
    app.add_handler(CallbackQueryHandler(show_category_items, pattern="^cat_"))
    app.add_handler(CallbackQueryHandler(browse_nav, pattern="^browse_"))
    app.add_handler(CallbackQueryHandler(report_listing, pattern="^report_"))
    app.add_handler(CallbackQueryHandler(start, pattern="^back_home$"))
    
    app.add_handler(conv_handler)
    app.add_handler(hire_conv)
    
    # JobQueue for price drops
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(check_price_drops, interval=PRICE_DROP_INTERVAL, first=10)
    else:
        print("⚠️ JobQueue not available! Price drops won't work.")
    
    print("🤖 Bot is running with FREE LEADS + REFERRAL system!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Render Web Service ke liye port bind
    port = int(os.environ.get("PORT", 10000))
    web_thread = threading.Thread(target=run_web_server, args=(port,), daemon=True)
    web_thread.start()
    print(f"✅ Dummy web server started on port {port} for Render health checks.")
    main()
