import os
import flask
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import psycopg2
from psycopg2.extras import RealDictCursor
import json
import uuid
from curl_cffi.requests import Session as CurlSession
import logging

# --- Configuration ---
logging.basicConfig(level=logging.INFO)

# --- Sentry Initialization ---
SENTRY_DSN = os.environ.get('SENTRY_DSN')
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=1.0,
    )

# --- Database Setup ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except psycopg2.OperationalError as e:
        logging.error(f"Could not connect to database: {e}")
        return None

def setup_database():
    """Creates the necessary tables in the database if they don't exist."""
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snappfood_tokens (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) UNIQUE NOT NULL,
                    token_info JSONB NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snappfood_vouchers (
                    id SERIAL PRIMARY KEY,
                    phone_number VARCHAR(20) NOT NULL,
                    title VARCHAR(255),
                    code VARCHAR(100) UNIQUE,
                    description TEXT,
                    expired_at VARCHAR(100),
                    fetched_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        logging.info("Database tables checked and created if necessary.")
    except Exception as e:
        logging.error(f"Database setup failed: {e}")
    finally:
        if conn:
            conn.close()

# --- Database Functions ---
def save_token_to_db(phone_number, token_info):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO snappfood_tokens (phone_number, token_info)
                VALUES (%s, %s)
                ON CONFLICT (phone_number) DO UPDATE SET token_info = EXCLUDED.token_info;
            """, (phone_number, json.dumps(token_info)))
            conn.commit()
    finally:
        conn.close()

def get_token_from_db(phone_number):
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT token_info FROM snappfood_tokens WHERE phone_number = %s;", (phone_number,))
            result = cur.fetchone()
            return result['token_info'] if result else None
    finally:
        conn.close()

def save_vouchers_to_db(phone_number, vouchers):
    conn = get_db_connection()
    if not conn: return
    saved_count = 0
    try:
        with conn.cursor() as cur:
            for v in vouchers:
                cur.execute("""
                    INSERT INTO snappfood_vouchers (phone_number, title, code, description, expired_at)
                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING;
                """, (phone_number, v.get('title'), v.get('customer_code'), v.get('description'), v.get('expired_at')))
                if cur.rowcount > 0:
                    saved_count += 1
            conn.commit()
    finally:
        conn.close()
    return saved_count

# --- Snappfood Logic (from original as.py) ---
SITE_CONFIGS = {
    "snappfood": {
        "otp_url": "https://snappfood.ir/mobile/v4/user/loginMobileWithNoPass",
        "login_url": "https://snappfood.ir/mobile/v2/user/loginMobileWithToken",
        "discounts_url": "https://snappfood.ir/mobile/v2/user/activeVouchers",
    }
}

def format_phone_number(phone):
    if not phone.startswith('0'):
        return '0' + phone
    return phone

def send_snappfood_otp(phone_number):
    config = SITE_CONFIGS['snappfood']
    formatted_phone = format_phone_number(phone_number)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    params = {"lat": "35.774", "long": "51.418", "optionalClient": "WEBSITE", "client": "WEBSITE", "deviceType": "WEBSITE", "appVersion": "8.1.1", "UDID": str(uuid.uuid4())}
    payload = {"cellphone": formatted_phone}
    try:
        with CurlSession(impersonate="chrome120") as session:
            response = session.post(config['otp_url'], params=params, data=payload, headers=headers)
            response.raise_for_status()
        return True
    except Exception as e:
        logging.error(f"Failed to send OTP for {formatted_phone}: {e}")
        return False

def verify_snappfood_otp_and_save_token(phone_number, otp_code):
    config = SITE_CONFIGS['snappfood']
    formatted_phone = format_phone_number(phone_number)
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    params = {"lat": "35.774", "long": "51.418", "optionalClient": "WEBSITE", "client": "WEBSITE", "deviceType": "WEBSITE", "appVersion": "8.1.1", "UDID": str(uuid.uuid4())}
    payload = {"cellphone": formatted_phone, "code": otp_code}
    try:
        with CurlSession(impersonate="chrome120") as session:
            response = session.post(config['login_url'], params=params, data=payload, headers=headers)
            response.raise_for_status()
            login_data = response.json()

        data = login_data.get("data", {})
        access_token = data.get("oauth2_token", {}).get("access_token")
        if access_token:
            token_info = {"token_type": "bearer", "access_token": access_token}
        else:
            return "❌ Login failed: Could not find token in response."

        save_token_to_db(formatted_phone, token_info)
        return f"✅ Snappfood account for {formatted_phone} added successfully!"
    except Exception as e:
        logging.error(f"Failed to verify OTP for {formatted_phone}: {e}")
        return f"❌ Login failed for {formatted_phone}. Please try again."

def fetch_and_save_vouchers(phone_number):
    token_info = get_token_from_db(phone_number)
    if not token_info:
        return f"ℹ️ No token found for {phone_number}. Please add the account first."

    config = SITE_CONFIGS['snappfood']
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Authorization": f"Bearer {token_info.get('access_token')}"
    }
    try:
        with CurlSession(impersonate="chrome120") as session:
            response = session.get(config['discounts_url'], headers=headers)
            response.raise_for_status()
            vouchers_data = response.json().get('data', {}).get('vouchers', [])
            if not vouchers_data:
                return f"✅ Checked {phone_number}: No new vouchers found."
            count = save_vouchers_to_db(phone_number, vouchers_data)
            return f"✅ Checked {phone_number}: Found and saved {count} new voucher(s)."
    except Exception as e:
        logging.error(f"Failed to fetch vouchers for {phone_number}: {e}")
        return f"❌ Failed to fetch vouchers for {phone_number}."

# --- Flask Web Server ---
app = flask.Flask(__name__)
user_states = {} # Simple in-memory storage for multi-step conversations

@app.route('/webhook', methods=['POST'])
def webhook():
    """This function handles messages from Telegram."""
    json_data = flask.request.get_json()
    try:
        # A basic way to get message details from Telegram's update
        chat_id = json_data['message']['chat']['id']
        message_text = json_data['message']['text']

        response_text = "I'm not sure how to respond to that. Try /start"

        if message_text.startswith('/start'):
            response_text = "Welcome! Use /add <phone_number> to add a Snappfood account, or /check <phone_number> to fetch vouchers."

        elif message_text.startswith('/add'):
            try:
                phone = message_text.split(' ')[1]
                if send_snappfood_otp(phone):
                    user_states[chat_id] = {'state': 'awaiting_otp', 'phone': phone}
                    response_text = f"An OTP has been sent to {phone}. Please reply with /otp <code>"
                else:
                    response_text = "❌ Failed to send OTP. Please check the phone number and try again."
            except IndexError:
                response_text = "Please provide a phone number. Usage: /add 09123456789"

        elif message_text.startswith('/otp'):
            user_context = user_states.get(chat_id)
            if user_context and user_context['state'] == 'awaiting_otp':
                try:
                    otp_code = message_text.split(' ')[1]
                    phone = user_context['phone']
                    response_text = verify_snappfood_otp_and_save_token(phone, otp_code)
                    del user_states[chat_id] # Clear the state
                except IndexError:
                    response_text = "Please provide the OTP code. Usage: /otp 12345"
            else:
                response_text = "Please use /add <phone_number> first."

        elif message_text.startswith('/check'):
            try:
                phone = message_text.split(' ')[1]
                response_text = fetch_and_save_vouchers(phone)
            except IndexError:
                response_text = "Please provide a phone number. Usage: /check 09
