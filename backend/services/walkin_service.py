# Backend/services/walkin_service.py
import uuid
import json
from datetime import datetime, timedelta
from db.redis_mongo import (
    redis_client,
    clinics_col,
    appointments_col
)
from queue_service.queue_engine import today_key
from queue_service.token_service import generate_token


# # ---------------- CONFIG ----------------
# BUFFER_CAPACITY = 1  # per buffer
# BUFFER_MINUTES = 15


# # ---------------- HELPERS ----------------
# def get_hour_buffers(now):
#     """
#     Example:
#     slots: 9:00, 9:30
#     buffer: 10:15
#     """
#     hour_start = now.replace(minute=0, second=0, microsecond=0)
#     buffers = []
#     for i in range(1, 6):
#         buffers.append(hour_start + timedelta(hours=i, minutes=BUFFER_MINUTES))
#     return buffers


def buffer_key(clinic_id):
    return f"buffer:late:{clinic_id}:{today_key()}"


def buffer_count_at_time(clinic_id, buffer_time):
    today = today_key()
    count = 0

    for item in redis_client.zrange(f"buffer:late:{clinic_id}:{today}", 0, -1):
        p = json.loads(item)

        slot_at = datetime.fromisoformat(p["slot_at"])

        clinic = clinics_col.find_one({"_id": clinic_id})
        buffer_min = clinic["slot_settings"].get("buffer_minutes", 15)

        derived_buffer_at = slot_at + timedelta(minutes=buffer_min)

        # Same buffer window (±1 min tolerance)
        if abs((derived_buffer_at - buffer_time).total_seconds()) < 60:
            count += 1

    return count


# ---------------- ESTIMATE ----------------
def estimate_walkin(data):
    clinic_id = data["clinic_id"]
    clinic = clinics_col.find_one({"_id": clinic_id})

    now = datetime.now()
    slot_minutes = clinic["slot_settings"]["slot_minutes"]
    buffer_min = clinic["slot_settings"].get("buffer_minutes", 15)
    buffer_capacity = clinic["slot_settings"].get("buffer_capacity", 1)

    # find next 6 buffers max (3 hours lookahead)
    for i in range(1, 7):
        candidate_buffer = (
            now.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=i)
            + timedelta(minutes=buffer_min)
        )

        if buffer_count_at_time(clinic_id, candidate_buffer) < buffer_capacity:
            wait_min = int((candidate_buffer - now).total_seconds() / 60)

            return {
                "allowed": True,
                "estimated_wait": wait_min,
                "buffer_at": candidate_buffer.strftime("%I:%M %p"),
                "buffer_iso": candidate_buffer.isoformat()
            }


    return {
        "status": "full",
        "message": "No buffer available in next few hours"
    }



# ---------------- CONFIRM ----------------
def confirm_walkin(data):
    """
    Creates Mongo appointment
    Auto check-in
    Adds to late buffer
    """

    required = ["clinic_id", "name", "age", "phone", "department", "buffer_iso"]
    for k in required:
        if k not in data:
            return {"error": f"missing_{k}"}

    clinic_id = data["clinic_id"]
    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        return {"error": "clinic_not_found"}

    now = datetime.now()
    buffer_at = datetime.fromisoformat(data["buffer_iso"])

    # ---- create appointment ----
    appointment_at = buffer_at

    clinic_open = datetime.combine(
        now.date(),
        datetime.strptime(clinic["slot_settings"]["start"], "%H:%M").time()
    )
    clinic_close = datetime.combine(
        now.date(),
        datetime.strptime(clinic["slot_settings"]["end"], "%H:%M").time()
    )

    checkin_token = uuid.uuid4().hex
    checkout_token = uuid.uuid4().hex
    display_token = generate_token(clinic_id)

    appt_doc = {
        "clinic_id": clinic_id,
        "phone": data["phone"],
        "name": data["name"],
        "age": data["age"],
        "department": data["department"],
        "date": appointment_at.strftime("%d %b"),
        "time": appointment_at.strftime("%I:%M %p"),
        "status": "confirmed",
        "created_at": now,
        "appointment_at": appointment_at,
        "is_walkin": True,

        "checkin": {
            "token_code": checkin_token,
            "valid_from": clinic_open,
            "valid_until": clinic_close,
            "checked_in": True,
            "checked_in_at": now,
            "token": display_token
        },

        "checkout": {
            "checked_out": False,
            "checked_out_at": None,
            "qr_sent": False,
            "token_code": checkout_token
        }
    }

    result = appointments_col.insert_one(appt_doc)
    appointment_id = str(result.inserted_id)

    # ---- push to late buffer ----
    payload = {
        "appointment_id": appointment_id,
        "token": display_token,
        "name": data["name"],
        "phone": data["phone"],
        "buffer_at": buffer_at.isoformat(),
        "slot_at": appointment_at.isoformat(),
        "checkin_at": now.isoformat()
    }

    redis_client.zadd(
        buffer_key(clinic_id),
        {json.dumps(payload): now.timestamp()}
    )

    return {
        "status": "confirmed",
        "token": appt_doc["checkin"]["token"],
        "next_buffer": appointment_at.strftime("%I:%M %p")
    }

