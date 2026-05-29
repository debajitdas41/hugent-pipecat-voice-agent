# Backend/utils/wa_utils.py
import requests
import os
import threading
import base64

# ─── Web-mode buffer (thread-local so concurrent requests don't interfere) ───
_web_buf = threading.local()

def set_web_mode(buffer: list):
    """Call before invoking the flow in web-chat mode. Responses accumulate in `buffer`."""
    _web_buf.messages = buffer

def clear_web_mode():
    _web_buf.messages = None

def _web_buffer():
    return getattr(_web_buf, "messages", None)

from db.redis_mongo import (
    get_user,
    upsert_user_if_not_exists,
    update_user_activity,
    get_session,
    create_session,
    update_session,
    mark_user_seen_in_redis,
    has_seen_before,
    redis_client,
    update_session as redis_update_session,
    mark_session_expired,
    has_expired_flag,
    users_col,
    clinics_col,   # mongo collection handle for saving appointments / drafts
    appointments_col
    
)

# META_API_URL = f"https://graph.facebook.com/v22.0/{os.getenv('WHATSAPP_PHONE_ID')}/messages"
# ACCESS_TOKEN = os.getenv("META_API_TOKEN")

class _MockResp:
    status_code = 200
    text = '{"ok":true}'

def send_whatsapp(payload, clinic_id):
    buf = _web_buffer()
    if buf is not None:
        buf.append(payload)
        return _MockResp()

    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        raise ValueError(f"Clinic {clinic_id} not found in database")
    
    if not clinic.get("whatsapp_credentials"):
        raise ValueError(f"Clinic {clinic_id} has no whatsapp_credentials configured")
    
    phone_id = clinic["whatsapp_credentials"].get("phone_number_id")
    ACCESS_TOKEN = clinic["whatsapp_credentials"].get("access_token")
    
    if not phone_id or not ACCESS_TOKEN:
        raise ValueError(f"Clinic {clinic_id} has incomplete WhatsApp credentials")
    
    META_API_URL = f"https://graph.facebook.com/v22.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    resp = requests.post(META_API_URL, json=payload, headers=headers)
    print("WA Resp:", resp.status_code, resp.text)
    return resp

def send_qr_to_whatsapp(phone: str, qr_bytes: bytes, clinic_id):
    """
    1) Upload QR image to WhatsApp servers
    2) Send it as an image message to the given phone
    """
    buf = _web_buffer()
    if buf is not None:
        buf.append({
            "type": "_qr_image",
            "data": base64.b64encode(qr_bytes).decode()
        })
        return _MockResp()

    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        raise ValueError(f"Clinic {clinic_id} not found in database")
    
    if not clinic.get("whatsapp_credentials"):
        raise ValueError(f"Clinic {clinic_id} has no whatsapp_credentials configured")
    
    phone_id = clinic["whatsapp_credentials"].get("phone_number_id")
    ACCESS_TOKEN = clinic["whatsapp_credentials"].get("access_token")
    
    if not phone_id or not ACCESS_TOKEN:
        raise ValueError(f"Clinic {clinic_id} has incomplete WhatsApp credentials")

    # 1️⃣ Upload media
    upload_url = f"https://graph.facebook.com/v22.0/{phone_id}/media"
    files = {
        "file": ("qr.png", qr_bytes, "image/png")
    }
    data = {
        "messaging_product": "whatsapp"
    }
    upload_resp = requests.post(
        upload_url,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        files=files,
        data=data
    )
    print("WA Upload Resp:", upload_resp.status_code, upload_resp.text)
    
    if upload_resp.status_code != 200:
        raise RuntimeError(f"WhatsApp media upload failed: {upload_resp.status_code} {upload_resp.text}")
    
    upload_data = upload_resp.json()
    media_id = upload_data.get("id")

    if not media_id:
        raise RuntimeError(f"WhatsApp did not return media_id. Response: {upload_resp.text}")

    # 2️⃣ Send image to user
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "image",
        "image": {
            "id": media_id,
            "caption": "📌 Scan this QR at the clinic entrance to check-in."
        }
    }
    return send_whatsapp(payload, clinic_id)


