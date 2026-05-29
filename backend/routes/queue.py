# routes/queue.py
from flask import Blueprint, request, jsonify
from queue_service.queue_engine import call_next_patient, active_count
from db.redis_mongo import clinics_col, appointments_col
from utils.qr_utils import generate_qr_bytes
from utils.wa_utils import send_whatsapp, send_qr_to_whatsapp
from utils.url_utils import get_public_base_url
import time
import os
from dotenv import load_dotenv
from datetime import datetime

from utils.timezone_utils import now_in_clinic

load_dotenv()

queue_bp = Blueprint("queue_bp", __name__)

@queue_bp.route("/next", methods=["GET"])
def next_patient():
    clinic_id = request.args.get("clinic_id")

    if not clinic_id:
        return jsonify({
            "status": "error",
            "message": "clinic_id required"
        }), 400

    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        return jsonify({
            "status": "error",
            "message": "Clinic not found"
        }), 404

    result = call_next_patient(clinic_id)

    # 1️⃣ Slot capacity full OR nothing to call
    if not result:
        return jsonify({
            "status": "wait",
            "message": "No patient can be called now"
        }), 200

    patient = result["patient"]
    appointment_id = patient["appointment_id"]

    # 1️⃣ Fetch appointment
    appointment = appointments_col.find_one(
        {"_id": appointment_id}
    )

    if not appointment:
        return jsonify({
            "status": "error",
            "message": "Appointment not found"
        }), 404

    checkout_data = appointment.get("checkout", {})
    checkout_token = checkout_data.get("token_code")

    # 2️⃣ Build checkout URL
    base_url = get_public_base_url()
    checkout_url = f"{base_url}/checkout?apt_id={appointment_id}&code={checkout_token}"

    # 3️⃣ Generate QR
    qr_bytes = generate_qr_bytes(checkout_url)

    # 4️⃣ Send QR to WhatsApp
    send_qr_to_whatsapp(
        appointment["phone"],
        qr_bytes,
        clinic_id
    )

    appointments_col.update_one(
        {"_id": appointment_id},
        {"$set": {
            "checkout.qr_sent": True
        }}
    )


    return jsonify({
        "status": "called",
        
        "appointment_id": appointment_id,
        "name": patient["name"],
        
    }), 200


@queue_bp.route("/slot_tick", methods=["POST"])
def slot_tick():
    clinic_id = request.json.get("clinic_id")

    if not clinic_id:
        return jsonify({"status": "error", "message": "clinic_id required"}), 400
    
    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        return jsonify({
            "status": "error",
            "message": "Clinic not found"
        }), 404

    # 1️⃣ Check clinic open hours
    now = now_in_clinic(clinic).replace(tzinfo=None).time()
    start = datetime.strptime(
        clinic["slot_settings"]["start"], "%H:%M"
    ).time()
    end = datetime.strptime(
        clinic["slot_settings"]["end"], "%H:%M"
    ).time()

    if not (start <= now <= end):
        return jsonify({
            "status": "idle",
            "reason": "clinic_closed"
        }), 200

    # # 2️⃣ If doctor already busy → do nothing
    # if active_count(clinic_id) >= int(
    #     clinic["slot_settings"]["capacity_per_slot"]
    # ):
    #     return jsonify({
    #         "status": "idle",
    #         "reason": "capacity_full"
    #     }), 200
    

    # 1️⃣ Call SAME logic as /next
    result = call_next_patient(clinic_id)

    if not result:
        return jsonify({
            "status": "idle",
            "reason": "no_waiting"
        }), 200

    # 2️⃣ If someone was activated, notify them
    
    patient = result["patient"]
    appointment_id = patient["appointment_id"]

    appointment = appointments_col.find_one(
        {"_id": appointment_id}
    )
    if not appointment:
        return jsonify({
            "status": "error",
            "message": "Appointment not found"
        }), 404

    checkout_token = appointment["checkout"]["token_code"]
    base_url = get_public_base_url()
    checkout_url = f"{base_url}/checkout?apt_id={appointment_id}&code={checkout_token}"

    qr_bytes = generate_qr_bytes(checkout_url)

    send_qr_to_whatsapp(
        appointment["phone"],
        qr_bytes,
        clinic_id
    )
    print(f"[SLOT_TICK] Activated {appointment_id} for clinic {clinic_id}")

    appointments_col.update_one(
        {"_id": appointment_id},
        {"$set": {
            "checkout.qr_sent": True
        }}
    )

    return jsonify({
        "status": "called",
        
        "appointment_id": appointment_id,
        "name": patient["name"]
        
    }), 200


