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
    except Exception as e:
        return f"❌ Error connecting to worker: {e}"

def send_telegram_message(chat_id, text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={'chat_id': chat_id, 'text': text})

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    if 'message' not in update: return "OK", 200

    chat_id = update['message']['chat']['id']
    text = update['message'].get('text', '')

    if text.startswith('/'):
        parts = text.split()
        command = parts[0]

        if command == '/start':
            send_telegram_message(chat_id, "Welcome! Use /add <service> <phone>")
        elif command == '/add':
            try:
                service, phone = parts[1].lower(), parts[2]
                user_states[chat_id] = {'service': service, 'phone': phone}
                send_telegram_message(chat_id, f"Please reply with the OTP for {service} on {phone}.")
            except IndexError:
                send_telegram_message(chat_id, "Usage: /add <service> <phone>")
        # Add other command handlers here (/list, /download) that would also call the worker

    elif chat_id in user_states:
        state = user_states.pop(chat_id)
        otp = text
        payload = {'command': 'login', 'params': {**state, 'otp': otp, 'chat_id': chat_id}}
        send_telegram_message(chat_id, f"Verifying OTP for {state['phone']}...")
        result = forward_task_to_worker(payload)
        send_telegram_message(chat_id, result)

    return "OK", 200
