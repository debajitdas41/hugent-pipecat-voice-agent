import os
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from db.pg import SessionLocal
from db.models import Appointment, AppointmentCheckin
from db.redis_mongo import clinics_col, redis_client
from utils.wa_handlers import send_text
from utils.timezone_utils import now_in_clinic

notifications_bp = Blueprint("notifications_bp", __name__)

DELAY_THRESHOLD_MIN = int(os.getenv("DELAY_THRESHOLD_MIN", "60"))
DELAY_NOTIFY_MIN_GAP = int(os.getenv("DELAY_NOTIFY_MIN_GAP", "30"))
DELAY_NOTIFY_SHIFT_MIN = int(os.getenv("DELAY_NOTIFY_SHIFT_MIN", "15"))


def _should_notify(appointment_id: str) -> bool:
    key = f"notify:appt:{appointment_id}:15m"
    return redis_client.set(key, "1", nx=True, ex=24 * 60 * 60)


def _should_notify_delay(appointment_id: str, eta_minutes: int) -> bool:
    last_eta_key = f"notify:late:{appointment_id}:eta"
    last_ts_key = f"notify:late:{appointment_id}:ts"

    last_eta = redis_client.get(last_eta_key)
    last_ts = redis_client.get(last_ts_key)

    if last_ts and (datetime.utcnow().timestamp() - float(last_ts)) < (DELAY_NOTIFY_MIN_GAP * 60):
        return False

    if last_eta is not None:
        try:
            if abs(eta_minutes - int(float(last_eta))) < DELAY_NOTIFY_SHIFT_MIN:
                return False
        except Exception:
            pass

    redis_client.set(last_eta_key, int(eta_minutes), ex=24 * 60 * 60)
    redis_client.set(last_ts_key, datetime.utcnow().timestamp(), ex=24 * 60 * 60)
    return True


def _send_reminders_for_clinic(clinic_id: str, window_minutes: int = 15):
    clinic = clinics_col.find_one({"_id": clinic_id})
    now = now_in_clinic(clinic).replace(tzinfo=None)
    start = now + timedelta(minutes=window_minutes)
    end = start + timedelta(minutes=1)

    sent = 0

    with SessionLocal() as db:
        stmt = (
            select(Appointment)
            .where(Appointment.clinic_id == clinic_id)
            .where(Appointment.status == "confirmed")
            .where(Appointment.appointment_at >= start)
            .where(Appointment.appointment_at < end)
        )
        appointments = list(db.execute(stmt).scalars().all())

        for appointment in appointments:
            if not appointment.phone:
                continue

            checkin = appointment.checkin or db.execute(
                select(AppointmentCheckin).where(
                    AppointmentCheckin.appointment_id == appointment.id
                )
            ).scalar_one_or_none()

            if checkin and checkin.checked_in:
                continue

            if not _should_notify(str(appointment.id)):
                continue

            message = (
                "⏰ Reminder: Your appointment is in 15 minutes.\n"
                f"📅 {appointment.date}  ⏰ {appointment.time}\n"
                "Please arrive on time."
            )
            send_text(appointment.phone, clinic_id, message)
            sent += 1

    return sent


def _send_delay_notifications_for_clinic(clinic_id: str):
    clinic = clinics_col.find_one({"_id": clinic_id})
    now = now_in_clinic(clinic).replace(tzinfo=None)

    delay_key = f"delay:clinic:{clinic_id}"
    current_delay = redis_client.get(delay_key)
    if current_delay is None:
        return 0

    try:
        current_delay = float(current_delay)
    except Exception:
        return 0

    if current_delay < DELAY_THRESHOLD_MIN:
        return 0

    sent = 0
    with SessionLocal() as db:
        stmt = (
            select(Appointment)
            .where(Appointment.clinic_id == clinic_id)
            .where(Appointment.status == "confirmed")
            .where(Appointment.appointment_at >= now)
        )
        appointments = list(db.execute(stmt).scalars().all())

        for appointment in appointments:
            checkin = appointment.checkin or db.execute(
                select(AppointmentCheckin).where(
                    AppointmentCheckin.appointment_id == appointment.id
                )
            ).scalar_one_or_none()

            if checkin and checkin.checked_in:
                continue

            scheduled = appointment.appointment_at
            if not scheduled:
                continue

            eta = scheduled + timedelta(minutes=current_delay)
            late_minutes = int((eta - scheduled).total_seconds() / 60)

            if late_minutes < DELAY_THRESHOLD_MIN:
                continue

            eta_in_minutes = int((eta - now).total_seconds() / 60)
            if eta_in_minutes < 0:
                continue

            if not _should_notify_delay(str(appointment.id), int(eta_in_minutes)):
                continue

            message = (
                "⚠️ Update: The clinic is running late today.\n"
                f"Your appointment is now expected around {eta.strftime('%I:%M %p')}.\n"
                "We appreciate your patience."
            )
            send_text(appointment.phone, clinic_id, message)
            sent += 1

    return sent


@notifications_bp.route("/notify_appointments", methods=["POST"])
def notify_appointments():
    payload = request.json or {}
    clinic_id = payload.get("clinic_id")
    window_minutes = int(payload.get("window_minutes", 15))

    if clinic_id:
        clinic = clinics_col.find_one({"_id": clinic_id})
        if not clinic:
            return jsonify({"status": "error", "message": "Clinic not found"}), 404
        sent = _send_reminders_for_clinic(clinic_id, window_minutes=window_minutes)
        return jsonify({"status": "ok", "sent": sent, "clinic_id": clinic_id}), 200

    clinics = clinics_col.find({}, {"_id": 1})
    total = 0
    for clinic in clinics:
        cid = str(clinic["_id"])
        total += _send_reminders_for_clinic(cid, window_minutes=window_minutes)

    return jsonify({"status": "ok", "sent": total}), 200


@notifications_bp.route("/notify_delays", methods=["POST"])
def notify_delays():
    payload = request.json or {}
    clinic_id = payload.get("clinic_id")

    if clinic_id:
        clinic = clinics_col.find_one({"_id": clinic_id})
        if not clinic:
            return jsonify({"status": "error", "message": "Clinic not found"}), 404
        sent = _send_delay_notifications_for_clinic(clinic_id)
        return jsonify({"status": "ok", "sent": sent, "clinic_id": clinic_id}), 200

    clinics = clinics_col.find({}, {"_id": 1})
    total = 0
    for clinic in clinics:
        cid = str(clinic["_id"])
        total += _send_delay_notifications_for_clinic(cid)

    return jsonify({"status": "ok", "sent": total}), 200