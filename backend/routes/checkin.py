#routes/checkin.py

import uuid
from flask import Blueprint, request, jsonify
from datetime import datetime
from db.redis_mongo import appointments_col, clinics_col
from queue_service.token_service import generate_token
from queue_service.queue_engine import add_to_queue
from utils.timezone_utils import now_in_clinic
from utils.qr_utils import generate_qr_bytes
from utils.wa_utils import send_qr_to_whatsapp
from utils.url_utils import get_public_base_url

checkin_bp = Blueprint("checkin_bp", __name__)

@checkin_bp.route("/checkin", methods=["GET"])
def checkin():
    apt_id = request.args.get("apt_id")
    code = request.args.get("code")

    if not apt_id or not code:
        return jsonify({"status": "error", "message": "Missing QR params"}), 400

    appointment = appointments_col.find_one({"_id": apt_id})
    if not appointment:
        return jsonify({"status": "error", "message": "Appointment not found"}), 404

    checkin_data = appointment.get("checkin", {})
    token_code = checkin_data.get("token_code")
    valid_from = checkin_data.get("valid_from")
    valid_until = checkin_data.get("valid_until")
    checked_in = checkin_data.get("checked_in", False)
    existing_display_token = checkin_data.get("token")
    checkout_token_code = uuid.uuid4().hex

    if existing_display_token:
        token = existing_display_token
    else:
        token = generate_token(appointment["clinic_id"])



    # 1️⃣ Validate token
    if code != token_code:
        return jsonify({"status": "error", "message": "Invalid QR"}), 401

    # 2️⃣ Validate already used
    if checked_in:
        t = checkin_data.get("checked_in_at")
        t_fmt = t.strftime("%I:%M %p") if t else ""
        return jsonify({
            "status": "already_checked",
            "message": f"Already checked in at {t_fmt}"
        }), 200

    # 3️⃣ Validate time window
    clinic = clinics_col.find_one({"_id": appointment["clinic_id"]})
    now = now_in_clinic(clinic).replace(tzinfo=None)
    print("Check-in attempt at:", now)
    # start = datetime.fromisoformat(valid_from)
    start = valid_from
    print("Valid from:", start)
    # end = datetime.fromisoformat(valid_until)
    end = valid_until
    print("Valid until:", end)  

    if not (start <= now <= end):
        return jsonify({"status": "expired", "message": "QR code expired"}), 403
    


    # 4️⃣ Mark check-in success
    appointments_col.update_one(
        {"_id": apt_id},
        {"$set": {
            "checkin.checked_in": True,
            "checkin.checked_in_at": now,
            "checkin.token": token,

            "checkout.token_code": checkout_token_code,
            "checkout.checked_out": False,
            "checkout.checked_out_at": None,
            "checkout.qr_sent": False
        }}
    )

    try:
        base_url = get_public_base_url()
        checkout_url = f"{base_url}/checkout?apt_id={apt_id}&code={checkout_token_code}"
        qr_bytes = generate_qr_bytes(checkout_url)
        send_qr_to_whatsapp(appointment.get("phone"), qr_bytes, appointment["clinic_id"])
        appointments_col.update_one(
            {"_id": apt_id},
            {"$set": {"checkout.qr_sent": True}},
        )
    except Exception:
        pass

    add_to_queue(apt_id, appointment["clinic_id"])

    return jsonify({
        "status": "success",
        "message": "Check-in successful",
        "name": appointment.get("name"),
        "token": token,
        "appointment_time": f"{appointment.get('date')} at {appointment.get('time')}"
    }), 200
