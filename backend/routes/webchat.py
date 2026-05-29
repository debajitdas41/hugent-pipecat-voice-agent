"""
Web Chat – simulates WhatsApp booking flow via browser.
Same backend flow. Zero changes to WhatsApp logic.
"""
import uuid

from flask import Blueprint, jsonify, request

from flows.webhook_flow import handle_webhook
from utils.wa_utils import set_web_mode, clear_web_mode

webchat_bp = Blueprint("webchat", __name__)

# Phone-number-id of the clinic (matches what's in the DB / WhatsApp flow)
_CLINIC_PHONE_NUMBER_ID = "901118879759626"


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_mock_payload(wa_msg: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "mock_waba",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "15550000000",
                        "phone_number_id": _CLINIC_PHONE_NUMBER_ID,
                    },
                    "messages": [wa_msg],
                },
                "field": "messages",
            }],
        }],
    }


def _wa_payload_to_web(payload: dict) -> dict:
    """Convert a WhatsApp send-payload into a web-renderable dict."""
    msg_type = payload.get("type")

    if msg_type == "_qr_image":
        return {"type": "qr_image", "data": payload["data"]}

    if msg_type == "text":
        return {"type": "text", "text": payload["text"]["body"]}

    if msg_type == "interactive":
        interactive = payload["interactive"]
        itype = interactive.get("type")
        body_text = interactive.get("body", {}).get("text", "")

        if itype == "button":
            buttons = [
                {"id": b["reply"]["id"], "title": b["reply"]["title"]}
                for b in interactive.get("action", {}).get("buttons", [])
            ]
            return {"type": "buttons", "text": body_text, "buttons": buttons}

        if itype == "list":
            action = interactive.get("action", {})
            rows = [
                {"id": r["id"], "title": r["title"], "description": r.get("description", "")}
                for s in action.get("sections", [])
                for r in s.get("rows", [])
            ]
            return {
                "type": "list",
                "text": body_text,
                "button_text": action.get("button", "Choose"),
                "rows": rows,
            }

    # fallback – stringify
    return {"type": "text", "text": str(payload)}


# ── main endpoint ─────────────────────────────────────────────────────────────

@webchat_bp.route("/webchat/send", methods=["POST"])
def webchat_send():
    """
    Accepts:
      { session_id, message, btn_id?, list_id?, list_title? }
    Returns:
      { session_id, responses: [...] }
    """
    body = request.json or {}
    session_id = body.get("session_id") or str(uuid.uuid4())

    # Use session_id as a fake phone number (max 15 chars, digits only for safety)
    phone = "9" + session_id.replace("-", "")[:14]

    message_text  = body.get("message", "")
    btn_id        = body.get("btn_id")
    list_id       = body.get("list_id")
    list_title    = body.get("list_title", "")

    # Build the WhatsApp-style message object
    if list_id:
        wa_msg = {
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": list_id, "title": list_title},
            },
            "from": phone,
            "id": "web_" + uuid.uuid4().hex,
        }
    elif btn_id:
        wa_msg = {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": btn_id, "title": message_text},
            },
            "from": phone,
            "id": "web_" + uuid.uuid4().hex,
        }
    else:
        wa_msg = {
            "type": "text",
            "text": {"body": message_text},
            "from": phone,
            "id": "web_" + uuid.uuid4().hex,
        }

    mock_payload = _build_mock_payload(wa_msg)

    # Run the same flow with response buffering
    response_buffer = []
    set_web_mode(response_buffer)
    try:
        class _Req:
            json = mock_payload
        handle_webhook(_Req())
    except Exception as exc:
        clear_web_mode()
        print("[WEBCHAT ERROR]", exc)
        return jsonify({"error": str(exc)}), 500
    finally:
        clear_web_mode()

    web_responses = [_wa_payload_to_web(p) for p in response_buffer]
    return jsonify({"session_id": session_id, "responses": web_responses})
