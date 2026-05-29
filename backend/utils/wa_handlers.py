from db.redis_mongo import mark_user_seen_in_redis
from utils.wa_utils import send_whatsapp


def send_text(phone, clinic_id, text):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text},
    }
    return send_whatsapp(payload, clinic_id)


def send_main_menu(phone, clinic_id, mark_seen=True):
    """
    Sends the main interactive menu. IMPORTANT: mark_seen=True will set the seen marker
    so has_seen_before() returns True for SEEN_TTL_SECONDS.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "How can I assist you today? 👇"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "book_apt", "title": "Book Appointment"}},
                    {"type": "reply", "reply": {"id": "services", "title": "Services"}},
                ]
            },
        },
    }
    resp = send_whatsapp(payload, clinic_id)
    # Mark seen only when we actually show the menu (this avoids the earlier bug).
    if mark_seen:
        try:
            mark_user_seen_in_redis(phone, clinic_id)
        except Exception as e:
            print("mark_user_seen_in_redis error:", e)
    return resp


def extract_btn_id(msg):
    try:
        if msg.get("type") == "interactive":
            interactive = msg.get("interactive", {})

            # 📌 1) LIST reply (used for slot selection & pagination)
            if "list_reply" in interactive:
                return interactive["list_reply"].get("id")

            # new WA structure
            if "button_reply" in interactive:
                return interactive["button_reply"].get("id")

            # fallback older
            if "button" in interactive:
                return interactive["button"].get("reply", {}).get("id")

            # older fail-safe
            return (
                interactive.get("list_reply", {}).get("id")
                or interactive.get("button_reply", {}).get("id")
                or interactive.get("button", {}).get("reply", {}).get("id")
            )
    except Exception as e:
        print("extract_btn_id error:", e)

    return None
