import os
import flask
from flask import request
import requests
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import logging

logging.basicConfig(level=logging.INFO)
app = flask.Flask(__name__)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
VPS_URL = os.environ.get('VPS_URL')
API_KEY = os.environ.get('WORKER_API_KEY')

if SENTRY_DSN := os.environ.get('SENTRY_DSN'):
    sentry_sdk.init(dsn=SENTRY_DSN, integrations=[FlaskIntegration()], traces_sample_rate=1.0)

user_states = {}

def forward_task_to_worker(payload):
    headers = {'X-Api-Key': API_KEY}
    try:
        response = requests.post(f"{VPS_URL}/execute", json=payload, headers=headers, timeout=90)
        response.raise_for_status()
        return response.json().get('result', "Worker returned no result.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Could not connect to worker VPS: {e}")
        sentry_sdk.capture_exception(e)
        return "❌ Error: Could not connect to the processing server."

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {'chat_id': chat_id, 'text': text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        sentry_sdk.capture_exception(e)

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if 'message' not in update: return "OK", 200

    chat_id = update['message']['chat']['id']
    text = update['message'].get('text', '')

    if text.startswith('/'):
        parts = text.split()
        command = parts[0].lower()

        if command == '/start':
            send_telegram_message(chat_id, "Welcome! Use /add <service> <phone>")

        elif command == '/add':
            try:
                service, phone = parts[1].lower(), parts[2]

                send_telegram_message(chat_id, f"Requesting OTP for {phone}...")
                payload = {'command': 'send_otp', 'params': {'phone': phone, 'service': service}}
                result = forward_task_to_worker(payload)

                if "OTP Sent" in result:
                    user_states[chat_id] = {'service': service, 'phone': phone}
                    send_telegram_message(chat_id, f"✅ OTP sent successfully. Please reply with the code.")
                else:
                    send_telegram_message(chat_id, f"❌ Failed to send OTP. Worker response: {result}")

            except IndexError:
                send_telegram_message(chat_id, "Usage: /add <service> <phone_number>")

        # Add handlers for /list, /download etc. here

    elif chat_id in user_states: # This part handles the user's OTP reply
        state = user_states.pop(chat_id)
        otp = text
        send_telegram_message(chat_id, f"Verifying OTP for {state['phone']}...")
        payload = {'command': 'login', 'params': {**state, 'otp': otp, 'chat_id': chat_id}}
        result = forward_task_to_worker(payload)
        send_telegram_message(chat_id, result)

    return "OK", 200
