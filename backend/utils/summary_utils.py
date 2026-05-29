# Backend/utils/summary_utils.py
from db.redis_mongo import get_session
from utils.wa_utils import send_whatsapp


def send_summary(phone, clinic_id):
    """Sends appointment summary with edit + confirm + cancel buttons."""
    s = get_session(phone, clinic_id) or {}
    if s.get("channel") == "voice":
        return

    msg = (
        f"Please confirm your appointment:\n\n"
        f"👤 *{s.get('name','')}*\n"
        f"🔢 Age: *{s.get('age','')}*\n"
        f"🏥 Dept: *{s.get('department','')}*\n"
        f"📅 Date: *{s.get('date','')}*\n"
        f"⏰ Time: *{s.get('time','')}*\n\n"
        "Choose an option:"
    )

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": msg},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "edit_details", "title": "Edit Details"}},
                    {"type": "reply", "reply": {"id": "confirm_appointment", "title": "Confirm"}},
                    {"type": "reply", "reply": {"id": "cancel_appointment", "title": "Cancel"}}
                ]
            }
        }
    }

    send_whatsapp(payload, clinic_id)
