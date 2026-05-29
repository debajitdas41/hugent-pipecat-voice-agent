import json
import time
from datetime import datetime

from db.redis_mongo import (
    get_session,
    redis_client,
    update_session as redis_update_session,
    users_col,
    clinics_col,
    appointments_col,
)
from utils.date_utils import get_next_7_dates, normalize_date_text
from utils.timezone_utils import now_in_clinic
from utils.slots_utils import generate_slots_for_date
from utils.slots_booking import get_booked_count
from utils.summary_utils import send_summary
from utils.wa_utils import send_whatsapp
from utils.wa_handlers import send_text


SESSION_TTL_SECONDS = int(__import__("os").getenv("SESSION_TTL_SECONDS"))


def start_appointment_flow(phone, clinic_id):
    """Start appointment flow and ask for name."""
    print(f"[DEBUG] start_appointment_flow() called for {phone}")
    try:
        from db.redis_mongo import create_session

        create_session(phone, clinic_id, ttl=SESSION_TTL_SECONDS)
        print(f"[DEBUG] create_session() called for {phone}")
    except Exception as e:
        print(f"[ERROR] create_session failed for {phone}: {e}")

    try:
        redis_update_session(
            phone,
            clinic_id,
            {"current_flow": "appointment", "step": "ask_name", "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )
        print(f"[DEBUG] update_session() set appointment flow for {phone} / clinic={clinic_id}")
    except Exception as e:
        print(f"[ERROR] update_session failed for {phone}: {e}")

    send_text(phone, clinic_id, "Sure 😊 Let's book your appointment.\n\nWhat's your *full name*?")


def send_slots_for_date(phone, clinic_id, chosen_date, now_ts=None):
    """
    Generate & send slot list for a given date.
    Also stores pagination info in Redis.

    Returns:
        True  -> slots sent
        False -> no slots available
    """
    if now_ts is None:
        now_ts = str(time.time())

    clinic = clinics_col.find_one({"_id": clinic_id})
    slots = generate_slots_for_date(chosen_date, clinic)

    # Filter only available slots
    available_slots = [s for s in slots if not s["is_full"]]

    if not available_slots:
        send_text(phone, clinic_id, f"❌ No slots available for *{chosen_date}*.\nPlease pick another date.")
        # go back to ask_date
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_date", "last_step_time": now_ts},
            ttl=SESSION_TTL_SECONDS,
        )
        return False

    # Save pagination info
    redis_update_session(
        phone,
        clinic_id,
        {
            "slot_date": chosen_date,
            "slot_page": 0,
            "all_slots": json.dumps([s["time"] for s in available_slots]),
            "last_step_time": now_ts,
        },
        ttl=SESSION_TTL_SECONDS,
    )

    # First 9 slots
    first_page = available_slots[:9]
    rows = [{"id": f"SLOT::{s['time']}", "title": s["time"]} for s in first_page]

    if len(available_slots) > 9:
        rows.append({"id": "MORE_SLOTS", "title": "More Slots ⏭"})

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": f"🗓 Available slots on {chosen_date}\nSelect a time slot:"},
            "action": {"button": "Choose Time", "sections": [{"title": "Available Slots", "rows": rows}]},
        },
    }

    send_whatsapp(payload, clinic_id)
    return True


def handle_appointment_flow(phone, clinic_id, user_msg):
    """Continue appointment flow according to Redis session 'step'."""
    session = get_session(phone, clinic_id) or {}
    step = session.get("step", "ask_name")
    now_ts = str(time.time())

    if step == "ask_name":
        name = user_msg.strip()
        back_to = session.get("back_to")  # detect edit mode

        # always save name
        redis_update_session(phone, clinic_id, {"name": name, "last_step_time": now_ts}, ttl=SESSION_TTL_SECONDS)

        # mongo update
        if back_to == "summary":
            next_step_mongo = "confirm"  # for resume after edit
        else:
            next_step_mongo = "ask_age"  # normal flow

        users_col.update_one(
            {"phone": phone, "clinic_id": clinic_id},
            {
                "$set": {
                    "last_draft.name": name,
                    "last_draft.step": next_step_mongo,
                    "last_draft.updated_at": datetime.utcnow(),
                }
            },
        )

        # edit mode -> go to summary
        if back_to == "summary":
            redis_update_session(phone, clinic_id, {"step": "confirm", "back_to": ""}, ttl=SESSION_TTL_SECONDS)
            send_summary(phone, clinic_id)
            return

        # normal flow -> next question
        redis_update_session(phone, clinic_id, {"step": "ask_age", "last_step_time": now_ts}, ttl=SESSION_TTL_SECONDS)
        send_text(phone, clinic_id, f"Thanks {name.split()[0]}! What's your *age*?")
        return

    if step == "ask_age":
        age = user_msg.strip()
        back_to = session.get("back_to")

        # always save age
        redis_update_session(phone, clinic_id, {"age": age, "last_step_time": now_ts}, ttl=SESSION_TTL_SECONDS)

        # mongo update
        if back_to == "summary":
            next_step_mongo = "confirm"
        else:
            next_step_mongo = "ask_department"

        users_col.update_one(
            {"phone": phone, "clinic_id": clinic_id},
            {
                "$set": {
                    "last_draft.age": age,
                    "last_draft.step": next_step_mongo,
                    "last_draft.updated_at": datetime.utcnow(),
                }
            },
        )

        # edit mode -> go to summary
        if back_to == "summary":
            redis_update_session(phone, clinic_id, {"step": "confirm", "back_to": ""}, ttl=SESSION_TTL_SECONDS)
            send_summary(phone, clinic_id)
            return

        # normal flow -> next question
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_department", "last_step_time": now_ts},
            ttl=SESSION_TTL_SECONDS,
        )
        send_text(phone, clinic_id, "Which *department* would you like?\n• Dental\n• Skin\n• Ortho\n• General")
        return

    if step == "ask_department":
        dept = user_msg.strip()
        back_to = session.get("back_to")  # detect if user came from edit-summary

        # save to redis
        redis_update_session(phone, clinic_id, {"department": dept, "last_step_time": now_ts}, ttl=SESSION_TTL_SECONDS)

        # decide what to store in Mongo for resume
        if back_to == "summary":
            next_step_mongo = "confirm"  # resume should return to summary
        else:
            next_step_mongo = "ask_date"  # normal flow

        users_col.update_one(
            {"phone": phone, "clinic_id": clinic_id},
            {
                "$set": {
                    "last_draft.department": dept,
                    "last_draft.step": next_step_mongo,
                    "last_draft.updated_at": datetime.utcnow(),
                }
            },
        )

        # 🟢 If came from EDIT mode → back to summary
        if back_to == "summary":
            redis_update_session(
                phone,
                clinic_id,
                {"step": "confirm", "back_to": "", "last_step_time": now_ts},
                ttl=SESSION_TTL_SECONDS,
            )

            send_summary(phone, clinic_id)
            return

        # 🟣 Normal booking → ask for date
        clinic = clinics_col.find_one({"_id": clinic_id})
        date_options = get_next_7_dates(clinic)
        working_days = clinic["slot_settings"].get("working_days", [])
        closing_time = clinic["slot_settings"].get("end")

        filtered_dates = []
        now = now_in_clinic(clinic).replace(tzinfo=None)

        for d in date_options:
            date_obj = datetime.strptime(d["value"], "%d %b").replace(year=now.year)
            weekday_name = date_obj.strftime("%a")

            if weekday_name not in working_days:
                continue

            if date_obj.date() == now.date():
                closing_dt = datetime.strptime(closing_time, "%H:%M").replace(
                    year=now.year, month=now.month, day=now.day
                )
                if now >= closing_dt:
                    continue

            filtered_dates.append(d)

        rows = [{"id": f"DATE::{d['value']}", "title": d["label"]} for d in filtered_dates]
        rows.append({"id": "DATE_MORE", "title": "More Dates"})

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "📅 Please choose a date for your appointment"},
                "action": {"button": "Choose Date", "sections": [{"title": "Select Date", "rows": rows[:10]}]},
            },
        }

        send_whatsapp(payload, clinic_id)
        return

    if step == "ask_date":
        normalized = normalize_date_text(user_msg)

        if not normalized:
            send_text(
                phone,
                clinic_id,
                "⚠️ I didn't understand that date.\nExample: *5 Dec, 6 Dec, 10 Jan*\nOr pick from the buttons next time.",
            )
            return

        chosen_date = normalized

        redis_update_session(
            phone,
            clinic_id,
            {"date": chosen_date, "step": "select_slot", "last_step_time": now_ts},
            ttl=SESSION_TTL_SECONDS,
        )

        users_col.update_one(
            {"phone": phone, "clinic_id": clinic_id},
            {
                "$set": {
                    "last_draft.date": chosen_date,
                    "last_draft.step": "select_slot",
                    "last_draft.updated_at": datetime.utcnow(),
                }
            },
        )

        send_slots_for_date(phone, clinic_id, chosen_date, now_ts=now_ts)
        return

    if step == "select_slot":
        return

    if step == "confirm":
        send_summary(phone, clinic_id)
        return
