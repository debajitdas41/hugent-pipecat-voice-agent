#routes/checkout.py
from flask import Blueprint, request, jsonify
from datetime import datetime
import os

from db.redis_mongo import redis_client, appointments_col, clinics_col
from queue_service.queue_engine import call_next_patient, today_key
import requests
from dotenv import load_dotenv
from utils.url_utils import get_public_base_url
from utils.timezone_utils import now_in_clinic

load_dotenv()

checkout_bp = Blueprint("checkout_bp", __name__)

DELAY_EMA_ALPHA = float(os.getenv("DELAY_EMA_ALPHA", "0.2"))

@checkout_bp.route("/checkout", methods=["GET"])
def checkout():
    apt_id = request.args.get("apt_id")
    code = request.args.get("code")

    if not apt_id or not code:
        return jsonify({"status": "error", "message": "Missing params"}), 400

    appointment = appointments_col.find_one({"_id": apt_id})
    if not appointment:
        return jsonify({"status": "error", "message": "Appointment not found"}), 404

    checkout_data = appointment.get("checkout", {})
    clinic_id = appointment["clinic_id"]

    # 1️⃣ Validate token
    if code != checkout_data.get("token_code"):
        return jsonify({"status": "error", "message": "Invalid checkout QR"}), 401

    # 2️⃣ Already checked out
    if checkout_data.get("checked_out"):
        return jsonify({
            "status": "already_checked_out",
            "message": "Already checked out"
        }), 200

    # 3️⃣ Remove from ACTIVE
    active_key = f"active:{clinic_id}:{today_key()}:{apt_id}"
    redis_client.delete(active_key)
    print(f"[CHECKOUT] Removed ACTIVE for {apt_id}")


    # 4️⃣ Mark completed in DB
    clinic = clinics_col.find_one({"_id": clinic_id})
    now = now_in_clinic(clinic).replace(tzinfo=None)
    appointments_col.update_one(
        {"_id": apt_id},
        {"$set": {
            "status": "completed",
            "checkout.checked_out": True,
            "checkout.checked_out_at": now,

        }}
    )

    # Update clinic delay EMA (minutes)
    try:
        if appointment.get("appointment_at"):
            scheduled = appointment.get("appointment_at")
            delay_min = max((now - scheduled).total_seconds() / 60, 0)

            delay_key = f"delay:clinic:{clinic_id}"
            prev = redis_client.get(delay_key)
            prev_val = float(prev) if prev else 0.0
            new_val = (DELAY_EMA_ALPHA * delay_min) + ((1 - DELAY_EMA_ALPHA) * prev_val)
            redis_client.set(delay_key, round(new_val, 2))
    except Exception:
        pass

    # 🔥 trigger next
    base_url = get_public_base_url()
    requests.get(
        f"{base_url}/next",
        params={"clinic_id": clinic_id},
        timeout=3
    )

    return jsonify({
        "status": "checked_out",
        "message": "Checkout successful"
    }), 200
