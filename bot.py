import telebot
import psycopg2
import json
import os
import logging
import time
import boto3
from datetime import datetime, timedelta
from flask import Flask, request
import stripe
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
API_TOKEN = os.getenv('TELEGRAM_API_TOKEN', '7900055310:AAGswliYMf8-ZA8BhhQpES1Ju2oQollvko4')
ADMIN_ID = int(os.getenv('ADMIN_ID', 7933828542))
STRIPE_API_KEY = os.getenv('STRIPE_API_KEY')
WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
TEST_USER_ID = 7761809923  # Test user for picks
PROCESS_TYPE = os.getenv('PROCESS_TYPE', 'web')  # Default to 'web' if not specified
HEROKU_APP_NAME = os.getenv('HEROKU_APP_NAME', 'championyourpicks-0097fae15cdf')  # Heroku app name
BOT_USERNAME = os.getenv('BOT_USERNAME', 'ChampionYourPicksBot')  # Replace with your bot's username
DOMAIN = f'https://{HEROKU_APP_NAME}.herokuapp.com'  # Dynamic Heroku domain

# Validate required environment variables
required_vars = {'TELEGRAM_API_TOKEN': API_TOKEN, 'STRIPE_API_KEY': STRIPE_API_KEY, 'STRIPE_WEBHOOK_SECRET': WEBHOOK_SECRET, 'HEROKU_APP_NAME': HEROKU_APP_NAME, 'BOT_USERNAME': BOT_USERNAME}
for name, value in required_vars.items():
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")

# Initialize bot and Flask app
bot = telebot.TeleBot(API_TOKEN)
app = Flask(__name__)
stripe.api_key = STRIPE_API_KEY

# Initialize S3 client for backup
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
)
S3_BUCKET = os.getenv('AWS_S3_BUCKET', 'championyourpicks-backup')  # Default bucket name

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]  # Use StreamHandler on Heroku
)
logger = logging.getLogger(__name__)

# Database functions (using Heroku Postgres)
def init_db():
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users 
                     (user_id INTEGER PRIMARY KEY, 
                      subscription_end TEXT,
                      payment_status TEXT,
                      payment_link TEXT,
                      referral_code TEXT,
                      referred_by INTEGER,
                      stripe_session_id TEXT)''')  # Added stripe_session_id column
        conn.commit()
        conn.close()
        logger.info("Database initialized or updated")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")

def is_subscribed(user_id):
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT subscription_end FROM users WHERE user_id=%s", (user_id,))
        result = c.fetchone()
        conn.close()
        if result and result[0]:
            return datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S') > datetime.now()
        return False
    except Exception as e:
        logger.error(f"Error checking subscription for user {user_id}: {e}")
        return False

def update_subscription(user_id, days, payment_link="Manually Activated", session_id=None):
    try:
        end_date = datetime.now() + timedelta(days=days)
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, subscription_end, payment_status, payment_link, stripe_session_id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET subscription_end=%s, payment_status=%s, payment_link=%s, stripe_session_id=%s",
                  (user_id, end_date.strftime('%Y-%m-%d %H:%M:%S'), 'active', payment_link, session_id,
                   end_date.strftime('%Y-%m-%d %H:%M:%S'), 'active', payment_link, session_id))
        conn.commit()
        conn.close()
        bot.send_message(user_id, f"🏆 Your subscription has been activated! It’s active until {end_date.strftime('%Y-%m-%d')}! 🚀")
        logger.info(f"Subscription updated for user {user_id} for {days} days")
        return end_date
    except Exception as e:
        logger.error(f"Error updating subscription for user {user_id}: {e}")
        return None

def set_test_user_subscription(user_id):
    try:
        end_date = datetime.strptime('2025-12-31 23:59:59', '%Y-%m-%d %H:%M:%S')
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, subscription_end, payment_status) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET subscription_end=%s, payment_status=%s",
                  (user_id, end_date.strftime('%Y-%m-%d %H:%M:%S'), 'active', end_date.strftime('%Y-%m-%d %H:%M:%S'), 'active'))
        conn.commit()
        conn.close()
        logger.info(f"Test user {user_id} automatically subscribed until {end_date}")
    except Exception as e:
        logger.error(f"Error setting test user subscription for {user_id}: {e}")

def clean_expired_subscriptions():
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("UPDATE users SET payment_status='expired' WHERE subscription_end < %s AND payment_status='active'",
                  (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        conn.commit()
        conn.close()
        logger.info("Expired subscriptions cleaned")
    except Exception as e:
        logger.error(f"Error cleaning expired subscriptions: {e}")

def get_all_subscribers():
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE payment_status='active'")
        subscribers = [row[0] for row in c.fetchall()]
        conn.close()
        return subscribers
    except Exception as e:
        logger.error(f"Error fetching subscribers: {e}")
        return []

def get_all_users():
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT user_id, subscription_end, payment_status, payment_link, referral_code, referred_by, stripe_session_id FROM users")
        users = c.fetchall()
        conn.close()
        return [{"user_id": row[0], "subscription_end": row[1], "payment_status": row[2], "payment_link": row[3], "referral_code": row[4], "referred_by": row[5], "stripe_session_id": row[6]} for row in users]
    except Exception as e:
        logger.error(f"Error fetching all users: {e}")
        return []

def get_subscriber_details():
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT user_id, subscription_end, payment_status FROM users")
        subscribers = c.fetchall()
        conn.close()
        
        subscriber_info = []
        for sub_id, sub_end, status in subscribers:
            try:
                user = bot.get_chat(sub_id)
                name = user.first_name or "Unknown"
                username = f"@{user.username}" if user.username else "No Username"
                subscriber_info.append((sub_id, name, username, sub_end, status))
            except Exception as e:
                logger.error(f"Error fetching Telegram info for user {sub_id}: {e}")
                subscriber_info.append((sub_id, "Unknown", "No Username", sub_end, status))
        return subscriber_info
    except Exception as e:
        logger.error(f"Error fetching subscriber details: {e}")
        return []

def get_user_subscription(user_id):
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT subscription_end, payment_status FROM users WHERE user_id=%s", (user_id,))
        result = c.fetchone()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"Error fetching subscription for user {user_id}: {e}")
        return None

def get_user_by_session_id(session_id):
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE stripe_session_id=%s", (session_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"Error fetching user by session ID {session_id}: {e}")
        return None

def generate_referral_code(user_id):
    code = f"REF{user_id}{datetime.now().strftime('%H%M%S')}"
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("UPDATE users SET referral_code=%s WHERE user_id=%s", (code, user_id))
        conn.commit()
        conn.close()
        return code
    except Exception as e:
        logger.error(f"Error generating referral code for user {user_id}: {e}")
        return None

def use_referral_code(user_id, code):
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE referral_code=%s", (code,))
        referrer = c.fetchone()
        if referrer and referrer[0] != user_id:
            c.execute("UPDATE users SET referred_by=%s WHERE user_id=%s", (referrer[0], user_id))
            conn.commit()
            bot.send_message(referrer[0], "🎁 Someone used your referral code! You’ll get a bonus soon!")
            bot.send_message(user_id, "✅ Referral code applied! Enjoy your subscription!")
            logger.info(f"User {user_id} used referral code {code} from {referrer[0]}")
            conn.close()
            return True
        conn.close()
        return False
    except Exception as e:
        logger.error(f"Error using referral code for user {user_id}: {e}")
        return False

# Backup and restore functions using S3
def backup_users():
    try:
        users = get_all_users()
        # Save to a temporary file
        with open('/tmp/users_backup.json', 'w') as f:
            json.dump(users, f, indent=4)
        # Upload to S3
        s3_client.upload_file('/tmp/users_backup.json', S3_BUCKET, 'users_backup.json')
        logger.info("Backed up user data to S3")
    except Exception as e:
        logger.error(f"Error backing up user data to S3: {e}")

def restore_users_from_backup():
    try:
        # Download from S3
        s3_client.download_file(S3_BUCKET, 'users_backup.json', '/tmp/users_backup.json')
        with open('/tmp/users_backup.json', 'r') as f:
            users = json.load(f)
        
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        for user in users:
            c.execute("INSERT INTO users (user_id, subscription_end, payment_status, payment_link, referral_code, referred_by, stripe_session_id) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET subscription_end=%s, payment_status=%s, payment_link=%s, referral_code=%s, referred_by=%s, stripe_session_id=%s",
                      (user['user_id'], user['subscription_end'], user['payment_status'], user['payment_link'], user['referral_code'], user['referred_by'], user.get('stripe_session_id'),
                       user['subscription_end'], user['payment_status'], user['payment_link'], user['referral_code'], user['referred_by'], user.get('stripe_session_id')))
        conn.commit()
        conn.close()
        logger.info("Restored user data from S3 backup")
    except Exception as e:
        logger.error(f"Error restoring user data from S3 backup: {e}")

# Stripe Checkout Session Creation
def create_checkout_session(user_id, period):
    price = 5000 if period == "week" else 8000  # In cents
    days = 7 if period == "week" else 14
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': price,
                    'product_data': {
                        'name': f'{period.capitalize()} VIP Subscription',
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f'https://t.me/{BOT_USERNAME}?start=success_{{CHECKOUT_SESSION_ID}}',  # Redirect to Telegram bot
            cancel_url=f'https://t.me/{BOT_USERNAME}?start=cancel',  # Redirect to Telegram bot on cancel
            metadata={'user_id': str(user_id), 'days': str(days)}
        )
        # Store the session ID in the database
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("UPDATE users SET stripe_session_id=%s WHERE user_id=%s", (session.id, user_id))
        conn.commit()
        conn.close()
        return session.url
    except Exception as e:
        logger.error(f"Error creating checkout session for user {user_id}: {e}")
        return None

# Bot utility functions
def send_payment_link(user_id, period):
    if is_subscribed(user_id):
        bot.send_message(user_id, "🏆 You’re already a VIP member! Enjoy your perks!")
        return
    price = 50 if period == "week" else 80
    checkout_url = create_checkout_session(user_id, period)
    if not checkout_url:
        bot.send_message(user_id, "❌ Error generating payment link. Try again later.")
        return
    try:
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, payment_status, payment_link) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET payment_status=%s, payment_link=%s",
                  (user_id, 'pending', checkout_url, 'pending', checkout_url))
        conn.commit()
        conn.close()
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(f"Pay ${price}/{period}", url=checkout_url))
        markup.add(telebot.types.InlineKeyboardButton("🔙 Back to Menu", callback_data='back_to_main'))
        bot.send_message(user_id, f"💰 Unlock premium sports picks for just ${price}/{period}! Click below:", reply_markup=markup)
        logger.info(f"Pending subscription set for user {user_id} with {period} plan")
    except Exception as e:
        logger.error(f"Error setting pending subscription for user {user_id}: {e}")

# Preformatted picks template
def format_picks(nba_picks, nfl_picks, mlb_picks, parlay_pick):
    current_date = datetime.now().strftime('%Y-%m-%d')
    formatted_picks = f"📢 Exclusive Sports Picks – {current_date}\n\n"
    formatted_picks += "🔥 Top Analyst Picks 🔥\n\n"
    formatted_picks += "🏀 NBA Picks\n"
    for pick in nba_picks:
        formatted_picks += f"✅ {pick}\n"
    formatted_picks += "\n"
    formatted_picks += "🏈 NFL Picks\n"
    for pick in nfl_picks:
        formatted_picks += f"✅ {pick}\n"
    formatted_picks += "\n"
    formatted_picks += "⚾ MLB Picks\n"
    for pick in mlb_picks:
        formatted_picks += f"✅ {pick}\n"
    formatted_picks += "\n"
    formatted_picks += f"🎯 Expert Parlay of the Day\n"
    formatted_picks += f"💰 {parlay_pick}\n\n"
    formatted_picks += "🔔 Risk Management Tip: Always bet responsibly and manage your bankroll wisely.\n\n"
    formatted_picks += "🚀 Stay ahead. Stay winning!"
    return formatted_picks

SPORTS_PICKS = {
    'nba': ["Lakers +5.5 (-110)", "Warriors ML (-120)"],
    'nfl': ["Chiefs -3 (-105)", "Bills Over 48.5 (-115)"],
    'mlb': ["Yankees ML (-130)", "Dodgers -1.5 (+150)"],
    'parlay': "Lakers ML + Chiefs -3 (+250)"
}

def get_sports_menu():
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🏀 NBA", callback_data='sport_nba'),
        telebot.types.InlineKeyboardButton("🏈 NFL", callback_data='sport_nfl'),
        telebot.types.InlineKeyboardButton("⚾ MLB", callback_data='sport_mlb'),
        telebot.types.InlineKeyboardButton("🏒 NHL", callback_data='sport_nhl'),
        telebot.types.InlineKeyboardButton("🎾 Tennis", callback_data='sport_tennis'),
        telebot.types.InlineKeyboardButton("🔙 Back to Menu", callback_data='back_to_main')
    )
    return markup

# Menu generation
def get_user_menu(user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    if is_subscribed(user_id):
        markup.add(telebot.types.InlineKeyboardButton("🏀 Today’s Hot Picks", callback_data='picks'))
        markup.add(telebot.types.InlineKeyboardButton("📰 Latest Sports Buzz", callback_data='news'))
        markup.add(telebot.types.InlineKeyboardButton("📅 My Subscription", callback_data='status'))
        markup.add(telebot.types.InlineKeyboardButton("🎁 Refer a Friend", callback_data='referral'))
        markup.add(telebot.types.InlineKeyboardButton("❓ Help & Support", callback_data='help'))
    else:
        markup.add(telebot.types.InlineKeyboardButton("💸 Weekly VIP ($50)", callback_data='sub_weekly'))
        markup.add(telebot.types.InlineKeyboardButton("💎 Bi-Weekly Elite ($80)", callback_data='sub_biweekly'))
        markup.add(telebot.types.InlineKeyboardButton("🎁 Use Referral Code", callback_data='use_referral'))
        markup.add(telebot.types.InlineKeyboardButton("❓ Learn More", callback_data='help'))
    return markup

def get_admin_menu():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📤 Send Picks", callback_data='admin_sendpicks'))
    markup.add(telebot.types.InlineKeyboardButton("👥 View Subscribers", callback_data='admin_viewsubs'))
    markup.add(telebot.types.InlineKeyboardButton("🗑️ Remove Subscriber", callback_data='admin_removesub'))
    markup.add(telebot.types.InlineKeyboardButton("✅ Manually Activate", callback_data='admin_activate'))
    return markup

def get_back_button(is_admin=False):
    markup = telebot.types.InlineKeyboardMarkup()
    callback = 'admin_back_to_main' if is_admin else 'back_to_main'
    markup.add(telebot.types.InlineKeyboardButton("🔙 Back to Menu", callback_data=callback))
    return markup

# Bot handlers
@bot.message_handler(commands=['start'])
def send_welcome(message):
    clean_expired_subscriptions()
    user_id = message.from_user.id
    command = message.text.split()

    # Handle redirection from Stripe
    if len(command) > 1:
        param = command[1]
        if param.startswith('success_'):
            session_id = param.replace('success_', '')
            user_id_from_session = get_user_by_session_id(session_id)
            if user_id_from_session and user_id_from_session == user_id:
                if is_subscribed(user_id):
                    markup = get_user_menu(user_id)
                    bot.reply_to(message, "🎉 Payment successful! Your subscription is active. Welcome to the VIP club!", reply_markup=markup)
                else:
                    markup = get_user_menu(user_id)
                    bot.reply_to(message, "✅ Payment received! Your subscription should be active shortly. If not, please contact support.", reply_markup=markup)
            else:
                bot.reply_to(message, "❌ Invalid session ID or user mismatch. Please contact support.")
            return
        elif param == 'cancel':
            markup = get_user_menu(user_id)
            bot.reply_to(message, "❌ Payment was cancelled. You can try again anytime!", reply_markup=markup)
            return

    if user_id == TEST_USER_ID:
        set_test_user_subscription(user_id)

    if user_id == ADMIN_ID:
        markup = get_admin_menu()
        bot.reply_to(message, "👑 Welcome, Admin! Manage your empire:", reply_markup=markup)
    else:
        markup = get_user_menu(user_id)
        welcome_text = ("🏆 Welcome to Sports Picks Heaven! 🏆\n"
                        "Unlock expert picks for ALL sports - NBA, NFL, MLB, NHL, Tennis & more! 🏀🏈⚾\n"
                        "Join the VIP club and win BIG! 💰 Get these boys 🫡")
        bot.reply_to(message, welcome_text, reply_markup=markup)
    logger.info(f"User {user_id} started bot")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data
    is_admin = user_id == ADMIN_ID
    back_button = get_back_button(is_admin)

    # User commands
    if data == 'sub_weekly':
        send_payment_link(user_id, "week")
    elif data == 'sub_biweekly':
        send_payment_link(user_id, "bi-weekly")
    elif data == 'picks':
        if not is_subscribed(user_id):
            bot.answer_callback_query(call.id, "🔒 Subscribe to unlock premium picks!")
            return
        bot.send_message(user_id, "🏟️ Choose your sport for today’s hottest picks:", reply_markup=get_sports_menu())
    elif data.startswith('sport_'):
        if not is_subscribed(user_id):
            bot.answer_callback_query(call.id, "🔒 Subscribe to unlock premium picks!")
            return
        sport = data.split('_')[1]
        picks = SPORTS_PICKS.get(sport, ["Picks not available for this sport yet!"])
        formatted_picks = format_picks(
            SPORTS_PICKS['nba'],
            SPORTS_PICKS['nfl'],
            SPORTS_PICKS['mlb'],
            SPORTS_PICKS['parlay']
        )
        bot.send_message(user_id, formatted_picks, reply_markup=back_button)
        logger.info(f"{sport.upper()} picks sent to user {user_id}")
    elif data == 'back_to_main':
        bot.send_message(user_id, "🏆 Back to main menu:", reply_markup=get_user_menu(user_id))
    elif data == 'admin_back_to_main':
        bot.send_message(user_id, "👑 Back to admin menu:", reply_markup=get_admin_menu())
    elif data == 'news':
        if not is_subscribed(user_id):
            bot.answer_callback_query(call.id, "🔒 Subscribe to get the latest news!")
            return
        news = """
        📰 Hot Sports Updates 📰
        1. NBA Finals set for June! 🏀
        2. NFL Draft rumors buzzing! 🏈
        3. MLB season opener announced! ⚾
        4. NHL playoffs heating up! 🏒
        5. Wimbledon dates confirmed! 🎾
        Stay ahead of the game! 🏆
        """
        bot.send_message(user_id, news, reply_markup=back_button)
        logger.info(f"News sent to user {user_id}")
    elif data == 'status':
        sub_info = get_user_subscription(user_id)
        if sub_info and sub_info[1] == 'active':
            end_date = sub_info[0]
            bot.send_message(user_id, f"📅 Your VIP Status:\nActive until {end_date}\nKeep dominating the bets! 🏆", reply_markup=back_button)
        else:
            bot.send_message(user_id, "😔 No active subscription. Join the VIP club now!", reply_markup=get_user_menu(user_id))
        logger.info(f"User {user_id} checked subscription status")
    elif data == 'referral':
        if not is_subscribed(user_id):
            bot.answer_callback_query(call.id, "🔒 Subscribe to get your referral code!")
            return
        code = generate_referral_code(user_id) or "Error generating code"
        bot.send_message(user_id, f"🎁 Your Referral Code: **{code}**\nShare with friends to earn bonuses!", reply_markup=back_button)
        logger.info(f"Referral code generated for user {user_id}")
    elif data == 'use_referral':
        bot.send_message(user_id, "🏆 Enter a referral code to use:", reply_markup=back_button)
        bot.register_next_step_handler(call.message, apply_referral_code)
    elif data == 'help':
        help_text = """
        🏆 Sports Picks Heaven 🏆
        - Expert picks for ALL sports! 🏀🏈⚾🏒🎾
        - Weekly ($50) or Bi-Weekly ($80) VIP plans
        - Refer friends for bonuses! 🎁
        - Questions? Contact +12023205120
        Let’s win BIG together! 🚀
        """
        bot.send_message(user_id, help_text, reply_markup=back_button)

    # Admin commands
    elif user_id == ADMIN_ID:
        if data == 'admin_sendpicks':
            bot.send_message(user_id, "📤 Type your picks below (any format, as many lines as you want):\n"
                                     "Example:\n"
                                     "NBA: Lakers +5.5 (-110)\n"
                                     "NFL: Chiefs -3 (-105)\n"
                                     "Parlay: Lakers ML + Chiefs -3 (+250)", reply_markup=back_button)
            bot.register_next_step_handler(call.message, broadcast_picks)
        elif data == 'admin_viewsubs':
            subscribers = get_subscriber_details()
            if not subscribers:
                bot.send_message(user_id, "👥 No subscribers found.", reply_markup=back_button)
                return
            response = "👥 Subscriber Details:\n"
            for sub_id, name, username, sub_end, status in subscribers:
                response += f"ID: {sub_id}, Name: {name}, Username: {username}, End: {sub_end}, Status: {status}\n"
            bot.send_message(user_id, response, reply_markup=back_button)
            logger.info("Admin viewed subscribers")
        elif data == 'admin_removesub':
            bot.send_message(user_id, "🗑️ Enter the user ID to remove:", reply_markup=back_button)
            bot.register_next_step_handler(call.message, remove_subscriber)
        elif data == 'admin_activate':
            bot.send_message(user_id, "✅ Enter the user ID and days to activate (e.g., '123456789 7' for 7 days):", reply_markup=back_button)
            bot.register_next_step_handler(call.message, manually_activate_subscription)
    else:
        bot.answer_callback_query(call.id, "🚫 Unauthorized action!")

def apply_referral_code(message):
    user_id = message.from_user.id
    code = message.text.strip()
    back_button = get_back_button()
    if use_referral_code(user_id, code):
        bot.send_message(user_id, "✅ Referral applied! Check back for bonuses after subscribing!", reply_markup=back_button)
    else:
        bot.send_message(user_id, "❌ Invalid or unavailable referral code. Try again!", reply_markup=back_button)

def broadcast_picks(message):
    if message.from_user.id != ADMIN_ID:
        return
    back_button = get_back_button(is_admin=True)
    
    picks_input = message.text.strip()
    if not picks_input:
        bot.send_message(message.chat.id, "❌ Please enter at least one pick!", reply_markup=back_button)
        return

    current_date = datetime.now().strftime('%Y-%m-%d')
    formatted_picks = f"📢 Sports Picks – {current_date}\n\n{picks_input}\n\n"
    formatted_picks += "🔔 Risk Management Tip: Always bet responsibly and manage your bankroll wisely.\n"
    formatted_picks += "🚀 Stay ahead. Stay winning!"

    # Get all active subscribers
    subscribers = get_all_subscribers()
    if not subscribers:
        bot.send_message(message.chat.id, "❌ No active subscribers found!", reply_markup=back_button)
        return

    successful_sends = 0
    for sub_id in subscribers:
        try:
            bot.send_message(sub_id, formatted_picks)
            successful_sends += 1
        except Exception as e:
            logger.error(f"Failed to send picks to {sub_id}: {e}")

    bot.send_message(message.chat.id, f"📤 Picks sent to {successful_sends} out of {len(subscribers)} active subscribers!", reply_markup=back_button)
    logger.info(f"Admin sent picks to {successful_sends} out of {len(subscribers)} active subscribers")

def remove_subscriber(message):
    if message.from_user.id != ADMIN_ID:
        return
    back_button = get_back_button(is_admin=True)
    try:
        user_id = int(message.text)
        DATABASE_URL = os.getenv('DATABASE_URL')
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        c = conn.cursor()
        c.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
        conn.commit()
        conn.close()
        bot.send_message(message.chat.id, f"🗑️ User {user_id} removed from subscribers!", reply_markup=back_button)
        logger.info(f"Admin removed user {user_id}")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid user ID. Please enter a number.", reply_markup=back_button)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error removing subscriber: {e}", reply_markup=back_button)
        logger.error(f"Error removing subscriber: {e}")

def manually_activate_subscription(message):
    if message.from_user.id != ADMIN_ID:
        return
    back_button = get_back_button(is_admin=True)
    try:
        user_id, days = map(int, message.text.split())
        if days <= 0:
            bot.send_message(message.chat.id, "❌ Days must be a positive number!", reply_markup=back_button)
            return
        
        end_date = update_subscription(user_id, days)
        if end_date:
            bot.send_message(message.chat.id, f"✅ Subscription activated for user {user_id} until {end_date.strftime('%Y-%m-%d')}", reply_markup=back_button)
            logger.info(f"Admin manually activated subscription for user {user_id} for {days} days")
        else:
            bot.send_message(message.chat.id, "❌ Failed to activate subscription!", reply_markup=back_button)
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid format. Use: 'user_id days' (e.g., '123456789 7')", reply_markup=back_button)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error activating subscription: {e}", reply_markup=back_button)
        logger.error(f"Error in manual activation: {e}")

# Webhook endpoint
@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.error(f"Webhook verification failed: {e}")
        return 'Invalid request', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        payment_status = session.get('payment_status')
        if payment_status != 'paid':
            logger.error(f"Payment not completed for session {session.get('id')}, status: {payment_status}")
            return 'Payment not completed', 400

        user_id = session.get('metadata', {}).get('user_id')
        days = int(session.get('metadata', {}).get('days', 7))  # Default to 7 if missing
        session_id = session.get('id')
        if not user_id:
            logger.error("No user_id in metadata")
            return 'No user_id', 400

        try:
            user_id = int(user_id)  # Ensure user_id is an integer
            update_subscription(user_id, days, session.get('payment_link_url', 'Stripe Webhook'), session_id)
            logger.info(f"Webhook updated subscription for user {user_id} with {days} days")
        except Exception as e:
            logger.error(f"Webhook processing error for user {user_id}: {e}")
            return 'Webhook processing error', 500
    return 'Success', 200

# Run bot and webhook server based on process type
if __name__ == "__main__":
    init_db()
    restore_users_from_backup()  # Restore user data from S3 on startup
    clean_expired_subscriptions()
    logger.info("Starting bot and webhook server...")

    try:
        if PROCESS_TYPE == 'web':
            # Run Flask server for web process
            port = int(os.getenv('PORT', 4242))  # Use Heroku's PORT
            app.run(host='0.0.0.0', port=port)
        elif PROCESS_TYPE == 'worker':
            # Run Telegram bot polling for worker process
            while True:
                try:
                    bot.polling(none_stop=True)
                except Exception as e:
                    logger.error(f"Bot polling error: {e}, restarting...")
                    time.sleep(5)  # Wait before retrying
    except KeyboardInterrupt:
        logger.info("Bot shutting down, creating backup...")
        backup_users()  # Backup user data to S3 on shutdown
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        backup_users()  # Backup user data to S3 on unexpected shutdown
        raise
