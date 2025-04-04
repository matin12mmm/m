import json
import logging
import sqlite3
import threading
import time
import random
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from instagrapi import Client
from instagrapi.exceptions import LoginRequired
from cryptography.fernet import Fernet

# تنظیمات لاگینگ
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# کلید رمزنگاری (توی پروژه واقعی باید توی فایل جدا و امن ذخیره بشه)
encryption_key = Fernet.generate_key()
cipher = Fernet(encryption_key)

# اتصال به پایگاه داده
conn = sqlite3.connect('users.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        ig_username TEXT,
        ig_password TEXT,
        session_json TEXT,
        post_id TEXT,
        target_number TEXT,
        message TEXT,
        non_follower_message TEXT,
        is_active INTEGER DEFAULT 0
    )
''')
conn.commit()

# تابع برای رمزنگاری رمز عبور
def encrypt_password(password):
    return cipher.encrypt(password.encode()).decode()

# تابع برای رمزگشایی رمز عبور
def decrypt_password(encrypted_password):
    return cipher.decrypt(encrypted_password.encode()).decode()

# تابع برای ذخیره و بارگذاری جلسه اینستاگرام
def save_session(cl, telegram_id):
    session_json = json.dumps(cl.get_settings())
    cursor.execute('UPDATE users SET session_json = ? WHERE telegram_id = ?', (session_json, telegram_id))
    conn.commit()

def load_session(telegram_id):
    cursor.execute('SELECT session_json FROM users WHERE telegram_id = ?', (telegram_id,))
    session_json = cursor.fetchone()[0]
    if session_json:
        cl = Client()
        cl.set_settings(json.loads(session_json))
        return cl
    return None

# تابع لاگین به اینستاگرام
def login_to_instagram(telegram_id, username, password):
    cl = Client()
    try:
        cl.login(username, password)
        save_session(cl, telegram_id)
        return cl
    except Exception as e:
        logger.error(f"Login failed for {username}: {e}")
        return None

# تابع برای گرفتن یا تازه‌سازی کلاینت اینستاگرام
def get_instagram_client(telegram_id, username, encrypted_password):
    cl = load_session(telegram_id)
    if cl:
        try:
            cl.get_timeline_feed()  # تست جلسه
            return cl
        except LoginRequired:
            logger.info(f"Session expired for {username}, re-logging in.")
    password = decrypt_password(encrypted_password)
    return login_to_instagram(telegram_id, username, password)

# تابع برای چک کردن کامنت‌ها و فرستادن پیام
def check_comments_and_send_message(telegram_id):
    cursor.execute('SELECT * FROM users WHERE telegram_id = ? AND is_active = 1', (telegram_id,))
    user = cursor.fetchone()
    if not user:
        return

    username, encrypted_password, _, post_id, target_number, message, non_follower_message = user[1:8]
    cl = get_instagram_client(telegram_id, username, encrypted_password)
    if not cl:
        return

    try:
        comments = cl.media_comments(post_id)
        for comment in comments:
            if comment.text.strip() == target_number:
                user_id = comment.user.pk
                is_following = cl.user_following(cl.user_id)  # لیست فالووینگ‌ها
                if user_id in is_following:
                    cl.direct_send(message, [user_id])
                    logger.info(f"Sent message to follower {user_id}")
                else:
                    cl.direct_send(non_follower_message, [user_id])
                    logger.info(f"Sent message to non-follower {user_id}")
                time.sleep(random.uniform(5, 10))  # تاخیر تصادفی
    except Exception as e:
        logger.error(f"Error checking comments for {username}: {e}")

# حلقه برای چک کردن کامنت‌ها
def comment_check_loop():
    while True:
        cursor.execute('SELECT telegram_id FROM users WHERE is_active = 1')
        active_users = cursor.fetchall()
        for user in active_users:
            check_comments_and_send_message(user[0])
        time.sleep(60)  # هر 60 ثانیه چک کن

# دستور /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('سلام! برای لاگین به اینستاگرام از /login username password استفاده کن.')

# دستور /login
async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    if len(context.args) != 2:
        await update.message.reply_text('خطا: لطفا نام کاربری و رمز عبور را وارد کنید. مثال: /login username password')
        return
    username, password = context.args
    encrypted_password = encrypt_password(password)
    cursor.execute('INSERT OR REPLACE INTO users (telegram_id, ig_username, ig_password) VALUES (?, ?, ?)',
                   (telegram_id, username, encrypted_password))
    conn.commit()
    if login_to_instagram(telegram_id, username, password):
        await update.message.reply_text('لاگین موفق بود!')
    else:
        await update.message.reply_text('لاگین ناموفق بود. لطفا اطلاعات رو چک کن.')

# دستور /setpost
async def setpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    if len(context.args) != 2:
        await update.message.reply_text('خطا: لطفا ID پست و عدد رو وارد کنید. مثال: /setpost post_id 123')
        return
    post_id, target_number = context.args
    cursor.execute('UPDATE users SET post_id = ?, target_number = ? WHERE telegram_id = ?',
                   (post_id, target_number, telegram_id))
    conn.commit()
    await update.message.reply_text('پست و عدد مورد نظر تنظیم شد.')

# دستور /setmessage
async def setmessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text('خطا: لطفا پیام رو وارد کنید. مثال: /setmessage سلام فالوور عزیز')
        return
    message = ' '.join(context.args)
    cursor.execute('UPDATE users SET message = ? WHERE telegram_id = ?', (message, telegram_id))
    conn.commit()
    await update.message.reply_text('پیام برای فالوورها تنظیم شد.')

# دستور /setnonfollowermessage
async def setnonfollowermessage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text('خطا: لطفا پیام رو وارد کنید. مثال: /setnonfollowermessage لطفا فالو کنید')
        return
    message = ' '.join(context.args)
    cursor.execute('UPDATE users SET non_follower_message = ? WHERE telegram_id = ?', (message, telegram_id))
    conn.commit()
    await update.message.reply_text('پیام برای غیرفالوورها تنظیم شد.')

# دستور /startbot
async def startbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    cursor.execute('UPDATE users SET is_active = 1 WHERE telegram_id = ?', (telegram_id,))
    conn.commit()
    await update.message.reply_text('ربات فعال شد.')

# دستور /stopbot
async def stopbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.message.from_user.id
    cursor.execute('UPDATE users SET is_active = 0 WHERE telegram_id = ?', (telegram_id,))
    conn.commit()
    await update.message.reply_text('ربات متوقف شد.')

# تابع اصلی
def main():
    # توکن ربات تلگرام رو اینجا بذار
    application = Application.builder().token("7608150876:AAH7kI3Rcb-4LhQv2DgtA1fYWy00zJgANhw").build()

    # اضافه کردن handlerها
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("setpost", setpost))
    application.add_handler(CommandHandler("setmessage", setmessage))
    application.add_handler(CommandHandler("setnonfollowermessage", setnonfollowermessage))
    application.add_handler(CommandHandler("startbot", startbot))
    application.add_handler(CommandHandler("stopbot", stopbot))

    # شروع ربات تلگرام
    threading.Thread(target=comment_check_loop, daemon=True).start()
    application.run_polling()

if __name__ == '__main__':
    main()
