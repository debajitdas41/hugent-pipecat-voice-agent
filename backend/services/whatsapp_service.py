# Backend/services/whatsapp_service.py
import requests
import os
from dotenv import load_dotenv

load_dotenv()

META_API_URL = f"https://graph.facebook.com/v22.0/{os.getenv('WHATSAPP_PHONE_ID')}/messages"
ACCESS_TOKEN = os.getenv("META_API_TOKEN")
WHATSAPP_CALLS_URL = f"https://graph.facebook.com/v22.0/{os.getenv('WHATSAPP_PHONE_ID')}/calls"

def send_whatsapp(payload):
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    return requests.post(META_API_URL, json=payload, headers=headers)


def send_text(phone, text):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    return send_whatsapp(payload)


def send_menu_buttons(phone):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "How can I assist you today? 👇"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "book_apt", "title": "Book Apt"}},
                    {"type": "reply", "reply": {"id": "services", "title": "Services"}}
                ]
            }
        }
    }
    return send_whatsapp(payload)

def send_sdp_answer_to_whatsapp(call_id: str, sdp: str):
    payload = {
        "messaging_product": "whatsapp",
        "call_id": call_id,
        "action": "accept",
        "session": {
            "sdp_type": "answer",
            "sdp": sdp
        }
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    resp = requests.post(WHATSAPP_CALLS_URL, json=payload, headers=headers)
    print("📨 Sent SDP answer to WhatsApp:", resp.status_code, resp.text)
