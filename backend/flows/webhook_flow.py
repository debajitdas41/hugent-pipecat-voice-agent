import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta

from flask import jsonify
import requests

import ws_state
from db.redis_mongo import (
    get_user,
    upsert_user_if_not_exists,
    get_session,
    create_session,
    update_session as redis_update_session,
    mark_user_seen_in_redis,
    has_seen_before,
    redis_client,
    mark_session_expired,
    has_expired_flag,
    users_col,
    clinics_col,
    appointments_col,
)
from flows.appointment_flow import (
    start_appointment_flow,
    send_slots_for_date,
    handle_appointment_flow,
)
from utils.date_utils import get_next_7_dates, normalize_date_text
from utils.timezone_utils import now_in_clinic
from utils.qr_utils import generate_qr_bytes
from utils.slots_booking import get_booked_count
from utils.slots_utils import generate_slots_for_date
from utils.summary_utils import send_summary
from utils.wa_handlers import extract_btn_id, send_main_menu, send_text
from utils.wa_utils import send_qr_to_whatsapp, send_whatsapp
from utils.welcome_msg import generate_welcome
from utils.url_utils import get_public_base_url

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS"))
SEEN_TTL_SECONDS = int(os.getenv("SEEN_TTL_SECONDS"))
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
SAAS_API_URL = os.getenv("SAAS_API_URL", "http://localhost:8000")
VOICE_WEBHOOK_URL = os.getenv("VOICE_WEBHOOK_URL", "http://localhost:19000/webhook")


def _call_watchdog(stage: str, **kwargs):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    meta = " ".join([f"{k}={v}" for k, v in kwargs.items() if v is not None])
    if meta:
        print(f"[{now}] [CALL-WEBHOOK] {stage} {meta}")
    else:
        print(f"[{now}] [CALL-WEBHOOK] {stage}")


def _api_request(method: str, path: str, *, payload: dict | None = None, params: dict | None = None) -> dict:
    url = f"{SAAS_API_URL}{path}"
    response = requests.request(method, url, json=payload, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def verify_webhook(request):
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == VERIFY_TOKEN:
        return challenge, 200
    return "Error: verification failed", 403


def handle_webhook(request):
    data = request.json
    try:
        entry = data.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})

        # ==================================================
        # 1️⃣ CALL EVENTS (NEW – WhatsApp Voice Calling)
        # ==================================================
        if "calls" in value:
            calls = value["calls"]
            call = calls[0]

            call_id = call.get("id")
            event = call.get("event")
            phone_number_id = value.get("metadata", {}).get("phone_number_id")

            _call_watchdog(
                "received",
                call_id=call_id,
                event=event,
                from_phone=call.get("from") or call.get("caller") or call.get("caller_id"),
                phone_number_id=phone_number_id,
            )

            print("📦 FULL CALL PAYLOAD:", json.dumps(call, indent=2))

            if event == "connect":
                print("📞 Incoming WhatsApp call")

            try:
                started_at = time.time()
                forward_resp = requests.post(VOICE_WEBHOOK_URL, json=data, timeout=5)
                latency_ms = int((time.time() - started_at) * 1000)
                print("📨 Forwarded call payload to voice service")
                _call_watchdog(
                    "forward_result",
                    call_id=call_id,
                    event=event,
                    status_code=forward_resp.status_code,
                    latency_ms=latency_ms,
                )
            except Exception as error:
                print("❌ Voice forward failed:", error)
                _call_watchdog("forward_failed", call_id=call_id, event=event, error=str(error))

            return jsonify({"status": "call_forwarded", "call_id": call_id}), 200

        # ==================================================
        # 2️⃣ MESSAGE STATUS UPDATES (DELIVERED / READ)
        # ==================================================
        if "statuses" in value:
            status = value["statuses"][0]

            print("\n📬 WHATSAPP STATUS UPDATE")
            print("Message ID:", status.get("id"))
            print("Status:", status.get("status"))  # sent / delivered / read

            return jsonify({"status": "status_received"}), 200

        messages = value.get("messages")
        if not messages:
            return jsonify({"status": "ignored"}), 200

        msg = messages[0]
        print("RAW WHATSAPP MESSAGE:", json.dumps(msg, indent=2))

        # 🟢 Identify which clinic this WhatsApp message belongs to
        phone_number_id = value.get("metadata", {}).get("phone_number_id")
        clinic = clinics_col.find_one({"whatsapp_phone_number_id": phone_number_id})

        if not clinic:
            print("❌ ERROR: Unknown clinic WhatsApp number:", phone_number_id)
            return jsonify({"status": "unknown_clinic"}), 200

        clinic_id = str(clinic["_id"])
        print("➡️ Message belongs to clinic:", clinic_id)

    except Exception:
        return jsonify({"status": "ignored"}), 200

    # ------------------------------------------------------
    # Extract basic info
    # ------------------------------------------------------
    phone = msg.get("from")
    btn_id = extract_btn_id(msg)
    text = msg.get("text", {}).get("body", "").strip() if msg.get("type") == "text" else ""
    is_greeting = text.lower() in ("hi", "hello", "hii", "hey", "👋")

    # Ensure user exists
    user_doc = get_user(phone, clinic_id)
    if not user_doc:
        user_doc = upsert_user_if_not_exists(phone, clinic_id)

    # ==================================================
    # STEP-5: Auto-delete last_draft when seen_ttl expired
    # ==================================================
    if not has_seen_before(phone, clinic_id):  # seen_ttl expired -> user coming after long gap
        draft = (user_doc or {}).get("last_draft") or {}
        if draft:  # delete only if last_draft exists
            users_col.update_one({"phone": phone, "clinic_id": clinic_id}, {"$set": {"last_draft": {}}})
            print("[CLEANUP] last_draft removed because seen_ttl expired")

    # Refresh seen window on any inbound message to prevent false "inactive" welcomes
    try:
        mark_user_seen_in_redis(phone, clinic_id, ttl_seconds=SEEN_TTL_SECONDS)
    except Exception:
        pass

    # ------------------------------------------------------
    # Load session
    # ------------------------------------------------------
    session = get_session(phone, clinic_id)
    elapsed = None

    if session:
        try:
            last_ts = float(session.get("last_step_time", "0") or "0")
            elapsed = time.time() - last_ts
        except Exception:
            elapsed = None

    # ------------------------------------------------------
    # Create session on first incoming message
    # ------------------------------------------------------
    if is_greeting:
        session = get_session(phone, clinic_id)
        if session is None:
            create_session(phone, clinic_id, ttl=SESSION_TTL_SECONDS)
            session = get_session(phone, clinic_id)  # refresh
        # now HI welcome message
        welcome = generate_welcome(is_returning=has_seen_before(phone, clinic_id), name=user_doc.get("name"))
        send_text(phone, clinic_id, welcome)
        send_main_menu(phone, clinic_id, mark_seen=True)
        return jsonify({"status": "from_hi"}), 200

    # ------------------------------------------------------
    # DEBUG BLOCK
    # ------------------------------------------------------
    print("\n--- DEBUG INFO ---")
    print("btn_id:", btn_id)
    print("text:", text)
    print("session:", session)
    print("has_seen_before:", has_seen_before(phone, clinic_id))

    try:
        print("SESSION TTL:", redis_client.ttl(f"session:{clinic_id}:{phone}"))
        print("SEEN TTL:", redis_client.ttl(f"seen:{clinic_id}:{phone}"))
        print("EXPIRED_FLAG TTL:", redis_client.ttl(f"expired_flag:{clinic_id}:{phone}"))
    except Exception:
        print("TTL ERROR")

    if session:
        print("last_step_time:", session.get("last_step_time"))
        print("elapsed:", elapsed)

    print("--- END DEBUG ---\n")

    # ==================================================
    # 🔍 STEP-1: Detect if user should get CONTINUE / FRESH START prompt
    # ==================================================
    draft = (user_doc or {}).get("last_draft") or {}
    print("Last draft:", draft)
    resume_applicable = False

    # CASE A: expired_flag exists (normal case)
    if has_expired_flag(phone, clinic_id) and draft.get("name") and draft.get("step"):
        resume_applicable = True

    # CASE B: expired_flag not set because session vanished BEFORE webhook ran
    elif (not session) and has_seen_before(phone, clinic_id) and draft.get("name") and draft.get("step"):
        resume_applicable = True
        print("Resume applicable: ", resume_applicable)

    # ==================================================
    # 1️⃣ EXPIRED WINDOW ACTIVE (45–60 sec)
    # ==================================================
    if btn_id not in ["resume_appointment", "fresh_start", "book_apt"] and has_seen_before(phone, clinic_id) and (
        session is None
    ):
        # CASE A: User already provided name earlier → show resume prompt
        if resume_applicable:
            user_name = draft.get("name", "").split()[0]
            prompt = f"{user_name}, do you want to continue your previous application?"

            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": prompt},
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": "resume_appointment", "title": "Continue"}},
                            {"type": "reply", "reply": {"id": "fresh_start", "title": "Fresh Start"}},
                        ]
                    },
                },
            }
            send_whatsapp(payload, clinic_id)
            return jsonify({"status": "resume_prompt"}), 200

        if is_greeting:
            welcome = generate_welcome(is_returning=True, name=user_doc.get("name"))
            send_text(phone, clinic_id, welcome)

        send_main_menu(phone, clinic_id, mark_seen=True)
        return jsonify({"status": "expired_window_menu"}), 200

    # ==================================================
    # 🕒 SLOT SELECTION BUTTON (SLOT::<time>)
    # ==================================================
    if btn_id and btn_id.upper().startswith("SLOT::"):
        chosen_time = btn_id.split("::", 1)[1]  # e.g., "10:30 AM"

        # Read from Redis (NOT Mongo)
        s = get_session(phone, clinic_id) or {}
        chosen_date = s.get("date")

        clinic = clinics_col.find_one({"_id": clinic_id})
        booked = get_booked_count(clinic_id, chosen_date, chosen_time)
        capacity = clinic["slot_settings"].get("capacity_per_slot", 1)

        # Slot already filled
        if booked >= capacity:
            send_text(phone, clinic_id, "❌ This slot just got filled.\nPlease select another time.")

            slots = generate_slots_for_date(chosen_date, clinic)
            available_slots = [s for s in slots if not s["is_full"]]
            rows = [{"id": f"SLOT::{s['time']}", "title": s["time"]} for s in available_slots]

            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": f"🗓 Slots on {chosen_date}\nSelect a time:"},
                    "action": {"button": "Choose Time", "sections": [{"title": "Slots", "rows": rows}]},
                },
            }
            send_whatsapp(payload, clinic_id)
            return jsonify({"status": "slot_full_refresh"}), 200

        # Slot available → save time
        redis_update_session(phone, clinic_id, {"time": chosen_time, "last_step_time": str(time.time())}, ttl=SESSION_TTL_SECONDS)

        users_col.update_one(
            {"phone": phone, "clinic_id": clinic_id},
            {"$set": {"last_draft.time": chosen_time, "last_draft.updated_at": datetime.utcnow()}},
        )

        # check if user came from EDIT mode
        s = get_session(phone, clinic_id) or {}
        back_to = s.get("back_to")

        # 🟢 EDIT MODE → return to summary after slot
        if back_to == "summary":
            redis_update_session(
                phone,
                clinic_id,
                {"step": "confirm", "back_to": "", "last_step_time": str(time.time())},
                ttl=SESSION_TTL_SECONDS,
            )

            send_summary(phone, clinic_id)
            return jsonify({"status": "slot_updated_summary"}), 200

        # 🟣 NORMAL BOOKING → return to confirmation summary
        redis_update_session(phone, clinic_id, {"step": "confirm", "last_step_time": str(time.time())}, ttl=SESSION_TTL_SECONDS)

        send_summary(phone, clinic_id)
        return jsonify({"status": "slot_selected"}), 200

    # PAGINATION — NEXT PAGE
    if btn_id == "MORE_SLOTS":
        data = get_session(phone, clinic_id) or {}
        all_slots = json.loads(data.get("all_slots", "[]"))
        total = len(all_slots)
        page = int(data.get("slot_page", 0)) + 1  # next page

        # loop to first page after last
        if page * 9 >= total:
            page = 0

        redis_update_session(
            phone,
            clinic_id,
            {"slot_page": page, "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )

        start = page * 9
        slice_slots = all_slots[start : start + 9]

        rows = [{"id": f"SLOT::{t}", "title": t} for t in slice_slots]

        # ------- PREV button (only if >1 page exists) -------
        if total > 9:
            rows.insert(0, {"id": "PREV_SLOTS", "title": "⬅ Previous Slots"})

        # ------- NEXT button (always when >1 page exists) -------
        rows.append({"id": "MORE_SLOTS", "title": "More Slots ⏭"})

        # enforce 10 row limit, always keep PREV + NEXT
        if len(rows) > 10:
            rows = rows[:8] + rows[-2:]

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": f"🗓 Available slots on {data.get('slot_date')}"},
                "action": {"button": "Choose Time", "sections": [{"title": f"Slots (Page {page+1})", "rows": rows}]},
            },
        }
        send_whatsapp(payload, clinic_id)
        return jsonify({"status": "page_changed"}), 200

    # PAGINATION — PREVIOUS PAGE
    if btn_id == "PREV_SLOTS":
        data = get_session(phone, clinic_id) or {}
        all_slots = json.loads(data.get("all_slots", "[]"))
        total = len(all_slots)
        page = int(data.get("slot_page", 0)) - 1  # previous page

        # loop to last page if pressed at first page
        if page < 0:
            page = (total - 1) // 9  # last page index

        redis_update_session(
            phone,
            clinic_id,
            {"slot_page": page, "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )

        start = page * 9
        slice_slots = all_slots[start : start + 9]

        rows = [{"id": f"SLOT::{t}", "title": t} for t in slice_slots]

        # PREV always shown if more than 1 page exists
        if total > 9:
            rows.insert(0, {"id": "PREV_SLOTS", "title": "⬅ Previous Slots"})
            rows.append({"id": "MORE_SLOTS", "title": "More Slots ⏭"})

        # enforce WhatsApp 10-row safety
        if len(rows) > 10:
            rows = rows[:8] + rows[-2:]

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": f"🗓 Available slots on {data.get('slot_date')}"},
                "action": {"button": "Choose Time", "sections": [{"title": f"Slots (Page {page+1})", "rows": rows}]},
            },
        }
        send_whatsapp(payload, clinic_id)
        return jsonify({"status": "page_changed"}), 200

    # ==================================================
    # 📅 DATE SELECTION BUTTON (DATE::<date>)
    # ==================================================
    if btn_id and btn_id.startswith("DATE::"):
        chosen = btn_id.split("::", 1)[1]  # example "4 Dec"
        back_to = session.get("back_to")  # to detect edit mode
        now_ts = str(time.time())

        # Save date to redis
        redis_update_session(phone, clinic_id, {"date": chosen, "last_step_time": now_ts}, ttl=SESSION_TTL_SECONDS)

        # Save to mongo (affects resume)
        if back_to == "summary":
            next_step_mongo = "confirm"  # resume should go to summary
        else:
            next_step_mongo = "select_slot"

        users_col.update_one(
            {"phone": phone, "clinic_id": clinic_id},
            {
                "$set": {
                    "last_draft.date": chosen,
                    "last_draft.step": next_step_mongo,
                    "last_draft.updated_at": datetime.utcnow(),
                }
            },
        )

        # Send slot picker (same for normal & edit)
        ok = send_slots_for_date(phone, clinic_id, chosen, now_ts=now_ts)
        if not ok:
            return jsonify({"status": "no_slots"}), 200

        # 🟢 If date edit → after slot selection summary should come
        if back_to == "summary":
            redis_update_session(
                phone,
                clinic_id,
                {"step": "select_slot", "back_to": "summary", "last_step_time": now_ts},
                ttl=SESSION_TTL_SECONDS,
            )
        else:
            redis_update_session(
                phone,
                clinic_id,
                {"step": "select_slot", "back_to": "", "last_step_time": now_ts},
                ttl=SESSION_TTL_SECONDS,
            )

        return jsonify({"status": "date_selected"}), 200

    # ==================================================
    # 🔁 USER WANTS TO TYPE DATE (More Dates)
    # ==================================================
    if btn_id == "DATE_MORE":
        session = get_session(phone, clinic_id) or {}
        week_page = int(session.get("date_week_page", 0)) + 1  # go to next week index

        clinic = clinics_col.find_one({"_id": clinic_id})
        closing_time = clinic["slot_settings"].get("end")
        working_days = clinic["slot_settings"].get("working_days", [])

        filtered_dates = []
        now = now_in_clinic(clinic).replace(tzinfo=None)

        # Generate next 14 days and filter working days
        for i in range(14):
            d = now + timedelta(days=i)
            weekday_name = d.strftime("%a")

            if weekday_name not in working_days:
                continue

            # skip today if clinic already closed
            if i == 0:
                closing_dt = datetime.strptime(closing_time, "%H:%M").replace(year=d.year, month=d.month, day=d.day)
                if now >= closing_dt:
                    continue

            day = d.strftime("%d").lstrip("0")
            formatted = f"{day} {d.strftime('%b')}"
            label = d.strftime("%a") + " • " + formatted
            filtered_dates.append({"label": label, "value": formatted})

        # Pagination → 7 working days per page
        start = week_page * 7
        end = start + 7
        page_dates = filtered_dates[start:end]

        # Loop back after last page
        if not page_dates:
            week_page = 0
            page_dates = filtered_dates[:7]

        # Save page so next tap goes forward
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_date", "date_week_page": week_page, "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )

        rows = [{"id": f"DATE::{d['value']}", "title": d["label"]} for d in page_dates]
        rows.append({"id": "DATE_MORE", "title": "More Dates ⏭"})

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "📅 Please choose a date"},
                "action": {"button": "Choose Date", "sections": [{"title": "Select Date", "rows": rows[:10]}]},
            },
        }
        send_whatsapp(payload, clinic_id)
        return jsonify({"status": "date_more"}), 200

    # ==================================================
    # ✏ EDIT BUTTON HANDLERS — FINAL
    # ==================================================
    if btn_id == "edit_details":
        rows = [
            {"id": "edit_name", "title": "Name"},
            {"id": "edit_age", "title": "Age"},
            {"id": "edit_dept", "title": "Department"},
            {"id": "edit_date", "title": "Date"},
            {"id": "edit_time", "title": "Time"},
        ]

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "📝 What do you want to edit?"},
                "action": {"button": "Choose", "sections": [{"title": "Edit Options", "rows": rows}]},
            },
        }
        send_whatsapp(payload, clinic_id)
        return jsonify({"status": "edit_details_list"}), 200

    if btn_id == "edit_name":
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_name", "back_to": "summary", "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )
        send_text(phone, clinic_id, "✏ Please enter your name again:")
        return jsonify({"status": "edit_name"}), 200

    # Edit Age
    if btn_id == "edit_age":
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_age", "back_to": "summary", "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )
        send_text(phone, clinic_id, "✏ Please enter your age again:")
        return jsonify({"status": "edit_age"}), 200

    # Edit Department
    if btn_id == "edit_dept":
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_department", "back_to": "summary", "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )
        send_text(phone, clinic_id, "Which *department* would you like?\n• Dental\n• Skin\n• Ortho\n• General")
        return jsonify({"status": "edit_dept"}), 200

    # Edit Date → MUST SHOW LIST (not text)
    if btn_id == "edit_date":
        redis_update_session(
            phone,
            clinic_id,
            {"step": "ask_date", "back_to": "summary", "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )

        clinic = clinics_col.find_one({"_id": clinic_id})
        date_options = get_next_7_dates(clinic)
        closing_time = clinic["slot_settings"].get("end")
        working_days = clinic["slot_settings"].get("working_days", [])

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
                "body": {"text": "📅 Please choose a date"},
                "action": {"button": "Choose Date", "sections": [{"title": "Select Date", "rows": rows[:10]}]},
            },
        }
        send_whatsapp(payload, clinic_id)
        return jsonify({"status": "edit_date"}), 200

    # Edit Time → show slot list again
    if btn_id == "edit_time":
        s = get_session(phone, clinic_id) or {}
        chosen_date = s.get("date")

        redis_update_session(
            phone,
            clinic_id,
            {"step": "select_slot", "back_to": "summary", "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )

        send_slots_for_date(phone, clinic_id, chosen_date)
        return jsonify({"status": "edit_time"}), 200

    # ==================================================
    # 2️⃣ CONTINUE SESSION IF STILL ACTIVE (<45 sec)
    # ==================================================
    if session and session.get("current_flow") == "appointment":
        if btn_id not in ["confirm_appointment", "cancel_appointment"]:
            if elapsed is not None and elapsed <= SESSION_TTL_SECONDS:
                handle_appointment_flow(phone, clinic_id, text)
                return jsonify({"status": "continued_session"}), 200

    # ==================================================
    # 3️⃣ BOOK APPOINTMENT BUTTON (patched)
    # ==================================================
    if btn_id == "book_apt":
        # if flow already active → continue
        if session and session.get("current_flow") == "appointment":
            handle_appointment_flow(phone, clinic_id, "")
            return jsonify({"status": "resume_appointment"}), 200

        # if session exists but flow not started → START FLOW (this was missing)
        if session and session.get("current_flow") == "":
            start_appointment_flow(phone, clinic_id)
            return jsonify({"status": "begin_appointment"}), 200

        # 3️⃣ Expired window welcome only if no active session
        if session is None and has_seen_before(phone, clinic_id):
            welcome = generate_welcome(is_returning=True, name=user_doc.get("name"))
            send_text(phone, clinic_id, welcome)
            send_main_menu(phone, clinic_id, mark_seen=True)
            create_session(phone, clinic_id, ttl=SESSION_TTL_SECONDS)
            return jsonify({"status": "expired_window_book_btn"}), 200

        # Otherwise start fresh
        start_appointment_flow(phone, clinic_id)
        return jsonify({"status": "fresh_booking"}), 200

    # ==================================================
    # 4️⃣ CONFIRM BUTTON
    # ==================================================
    if btn_id == "confirm_appointment":
        s = get_session(phone, clinic_id) or {}

        chosen_date = s.get("date")
        chosen_time = s.get("time")

        if not chosen_date or not chosen_time:
            send_text(phone, clinic_id, "⚠️ Please select a date and time before confirming.")
            return jsonify({"status": "missing_slot"}), 200

        if not s.get("name") or not s.get("age") or not s.get("department"):
            send_text(phone, clinic_id, "⚠️ Please complete your details before confirming.")
            return jsonify({"status": "missing_details"}), 200

        existing_appointment_id = s.get("appointment_id")
        if existing_appointment_id:
            send_text(phone, clinic_id, "✅ Appointment already confirmed. Please check the QR message.")
            return jsonify({"status": "already_confirmed"}), 200

        clinic = clinics_col.find_one({"_id": clinic_id})
        capacity = clinic["slot_settings"].get("capacity_per_slot", 1)
        booked = get_booked_count(clinic_id, chosen_date, chosen_time)

        # 🚨 Slot got filled before user confirmed
        if booked >= capacity:
            send_text(phone, clinic_id, f"❌ That slot *{chosen_time} on {chosen_date}* just got filled.\nPlease select another time.")

            # Move user back to slot selection
            redis_update_session(
                phone,
                clinic_id,
                {"step": "select_slot", "last_step_time": str(time.time())},
                ttl=SESSION_TTL_SECONDS,
            )

            # ─── Regenerate available slot list ───
            slots = generate_slots_for_date(chosen_date, clinic)
            available = [s for s in slots if not s["is_full"]]

            redis_update_session(
                phone,
                clinic_id,
                {"slot_date": chosen_date, "slot_page": 0, "all_slots": json.dumps([s["time"] for s in available])},
                ttl=SESSION_TTL_SECONDS,
            )

            first_page = [s["time"] for s in available][:9]
            rows = [{"id": f"SLOT::{t}", "title": t} for t in first_page]

            if len(available) > 9:
                rows.append({"id": "MORE_SLOTS", "title": "More Slots ⏭"})

            payload = {
                "messaging_product": "whatsapp",
                "to": phone,
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": f"🗓 Available slots on {chosen_date}\nSelect a different time:"},
                    "action": {"button": "Choose Time", "sections": [{"title": "Available Slots", "rows": rows}]},
                },
            }
            send_whatsapp(payload, clinic_id)
            return jsonify({"status": "slot_full_on_confirm"}), 200

        # 🟢 Slot is still free → confirm booking
        today = now_in_clinic(clinic).replace(tzinfo=None)
        appt_date = datetime.strptime(chosen_date, "%d %b").replace(year=today.year)

        # Parse "10:30 AM" → time
        appt_time = datetime.strptime(chosen_time, "%I:%M %p").time()

        appointment_at = datetime.combine(appt_date.date(), appt_time)

        # Clinic opening/closing (e.g. "10:00", "21:00")
        start_str = clinic["slot_settings"].get("start", "00:00")
        end_str = clinic["slot_settings"].get("end", "23:59")

        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()

        clinic_open_at = datetime.combine(appt_date.date(), start_time)
        clinic_close_at = datetime.combine(appt_date.date(), end_time)

        # 🕒 Check-in validity window
        EARLY_WINDOW_MIN = clinic["slot_settings"].get("early_window_minutes", 45)
        CLOSING_BUFFER = clinic["slot_settings"].get("closing_buffer_minutes", 30)

        valid_from = max(appointment_at - timedelta(minutes=EARLY_WINDOW_MIN), clinic_open_at)
        print("Valid from:", valid_from)
        valid_until = clinic_close_at - timedelta(minutes=CLOSING_BUFFER)
        print("Valid until:", valid_until)

        checkin_token = uuid.uuid4().hex

        appointment_payload = {
            "clinic_id": clinic_id,
            "user_id": user_doc.get("_id") if user_doc else None,
            "phone": phone,
            "name": s.get("name"),
            "age": s.get("age"),
            "department": s.get("department"),
            "date": chosen_date,
            "time": chosen_time,
            "status": "confirmed",
            "appointment_at": appointment_at.isoformat(),
            "is_walkin": False,
        }

        try:
            appointment_resp = _api_request("POST", "/api/appointments/reserve", payload=appointment_payload)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                send_text(
                    phone,
                    clinic_id,
                    f"❌ That slot *{chosen_time} on {chosen_date}* just got filled.\nPlease select another time.",
                )

                redis_update_session(
                    phone,
                    clinic_id,
                    {"step": "select_slot", "last_step_time": str(time.time())},
                    ttl=SESSION_TTL_SECONDS,
                )

                slots = generate_slots_for_date(chosen_date, clinic)
                available = [s for s in slots if not s["is_full"]]
                redis_update_session(
                    phone,
                    clinic_id,
                    {"slot_date": chosen_date, "slot_page": 0, "all_slots": json.dumps([s["time"] for s in available])},
                    ttl=SESSION_TTL_SECONDS,
                )

                first_page = [s["time"] for s in available][:9]
                rows = [{"id": f"SLOT::{t}", "title": t} for t in first_page]
                if len(available) > 9:
                    rows.append({"id": "MORE_SLOTS", "title": "More Slots ⏭"})

                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "interactive",
                    "interactive": {
                        "type": "list",
                        "body": {"text": f"🗓 Available slots on {chosen_date}\nSelect a different time:"},
                        "action": {"button": "Choose Time", "sections": [{"title": "Available Slots", "rows": rows}]},
                    },
                }
                send_whatsapp(payload, clinic_id)
                return jsonify({"status": "slot_full_on_confirm"}), 200
            raise

        appointment_id = appointment_resp["id"]

        redis_update_session(
            phone,
            clinic_id,
            {"appointment_id": appointment_id, "last_step_time": str(time.time())},
            ttl=SESSION_TTL_SECONDS,
        )

        _api_request("POST", f"/api/appointments/{appointment_id}/checkin")
        _api_request(
            "PATCH",
            f"/api/appointments/{appointment_id}/checkin",
            payload={
                "token_code": checkin_token,
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
                "checked_in": False,
                "checked_in_at": None,
                "token": None,
            },
        )
        appt = {
            "confirmed_at": datetime.utcnow().isoformat(),
            "name": s.get("name"),
            "age": s.get("age"),
            "department": s.get("department"),
            "date": chosen_date,
            "time": chosen_time,
        }

        user_id = user_doc.get("_id") if user_doc else None
        if user_id:
            link = _api_request(
                "GET",
                "/api/user-clinics",
                params={"user_id": user_id, "clinic_id": clinic_id},
            )
            link_id = link["id"]
            _api_request(
                "POST",
                f"/api/user-clinics/{link_id}/history",
                payload={"item": appt},
            )
            _api_request(
                "PATCH",
                f"/api/user-clinics/{link_id}",
                payload={"last_draft": {}},
            )

        # 🔗 Build secure check-in URL for QR
        base_url = get_public_base_url()
        checkin_url = f"{base_url}/checkin?apt_id={appointment_id}&code={checkin_token}"
        print("Check-in URL:", checkin_url)

        send_text(
            phone,
            clinic_id,
            "✅ Appointment Confirmed!\nOur team will contact you shortly.\n"
            f"📅 {s.get('date')}\n"
            f"⏰ {s.get('time')}\n"
            f"🏥 Dept: {s.get('department')}\n\n"
            "📌 Please bring this QR code to the clinic for self check-in.",
        )
        qr_bytes = generate_qr_bytes(checkin_url)
        time.sleep(1)
        send_qr_to_whatsapp(phone, qr_bytes, clinic_id)
        redis_client.delete(f"session:{clinic_id}:{phone}")
        time.sleep(3)
        send_main_menu(phone, clinic_id)
        return jsonify({"status": "appointment_confirmed"}), 200

    # ==================================================
    # 5️⃣ CANCEL BUTTON
    # ==================================================
    if btn_id == "cancel_appointment":
        send_text(phone, clinic_id, "❌ Appointment got cancelled.")
        users_col.update_one({"phone": phone, "clinic_id": clinic_id}, {"$set": {"last_draft": {}}})
        redis_client.delete(f"session:{clinic_id}:{phone}")

        send_main_menu(phone, clinic_id)
        return jsonify({"status": "appointment_cancelled"}), 200

    # ==================================================
    # ▶️ CONTINUE BUTTON — resume previous application
    # ==================================================
    if btn_id == "resume_appointment":
        draft = (user_doc or {}).get("last_draft") or {}
        print("Resuming draft:", draft)

        # create new session
        create_session(phone, clinic_id, ttl=SESSION_TTL_SECONDS)

        # restore saved fields to redis session
        restore = {
            "current_flow": "appointment",
            "step": draft.get("step", "ask_name"),
            "last_step_time": str(time.time()),
        }

        # optional saved inputs
        for key in ["name", "age", "department", "date", "time"]:
            if draft.get(key):
                restore[key] = draft[key]

        redis_update_session(phone, clinic_id, restore, ttl=SESSION_TTL_SECONDS)

        step = draft.get("step")
        print("Resuming at step:", step)
        # resume from exact last step
        if step == "ask_age":
            send_text(phone, clinic_id, f"Thanks {draft.get('name','').split()[0]}! What's your *age*?")
            return jsonify({"status": "resume_name"}), 200

        if step == "ask_department":
            send_text(phone, clinic_id, "Which *department* would you like?\n• Dental\n• Skin\n• Ortho\n• General")
            return jsonify({"status": "resume_age"}), 200

        if step == "ask_date":
            clinic = clinics_col.find_one({"_id": clinic_id})
            date_options = get_next_7_dates(clinic)
            closing_time = clinic["slot_settings"].get("end")
            working_days = clinic["slot_settings"].get("working_days", [])

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
                    "body": {"text": "🗓 Please choose a date for your appointment"},
                    "action": {"button": "Choose Date", "sections": [{"title": "Select Date", "rows": rows[:10]}]},
                },
            }
            send_whatsapp(payload, clinic_id)
            return jsonify({"status": "resume_date"}), 200

        # select_slot → use the same helper (no duplication)
        if step == "select_slot":
            chosen_date = draft.get("date")
            ok = send_slots_for_date(phone, clinic_id, chosen_date, now_ts=str(time.time()))
            if not ok:
                return jsonify({"status": "resume_slot_none"}), 200
            return jsonify({"status": "resume_slot"}), 200

        if step == "confirm":
            handle_appointment_flow(phone, clinic_id, "")
            return jsonify({"status": "resume_confirm"}), 200

        # fallback (should never happen)
        send_text(phone, clinic_id, "Let's continue, what is your *full name*?")
        return jsonify({"status": "resumed_session"}), 200

    # ==================================================
    # 🔄 FRESH START BUTTON — new flow from beginning
    # ==================================================
    if btn_id == "fresh_start":
        # 1️⃣ delete last draft
        users_col.update_one({"phone": phone, "clinic_id": clinic_id}, {"$set": {"last_draft": {}}})

        # 2️⃣ create brand new redis session
        create_session(phone, clinic_id, ttl=SESSION_TTL_SECONDS)

        # 3️⃣ refresh seen window so user won’t get welcome-back
        try:
            mark_user_seen_in_redis(phone, clinic_id, ttl_seconds=SEEN_TTL_SECONDS)
        except Exception:
            pass

        # 4️⃣ send full welcome message like first time
        welcome = generate_welcome(is_returning=False, name=None)
        send_text(phone, clinic_id, welcome)

        # 5️⃣ send menu to restart booking
        send_main_menu(phone, clinic_id, mark_seen=False)

        return jsonify({"status": "fresh_start_done"}), 200

    # ==================================================
    # 6️⃣ TEXT BOOKING TRIGGERS → ALWAYS FRESH
    # ==================================================
    if text.lower() in ("book", "book apt", "book appointment", "appointment"):
        start_appointment_flow(phone, clinic_id)
        return jsonify({"status": "fresh_booking_text"}), 200

    # ==================================================
    # 7️⃣ DEFAULT WELCOME LOGIC
    # ==================================================
    if has_seen_before(phone, clinic_id):
        if is_greeting:
            welcome = generate_welcome(is_returning=True, name=user_doc.get("name"))
            send_text(phone, clinic_id, welcome)
        send_main_menu(phone, clinic_id, mark_seen=True)
        return jsonify({"status": "welcome_back_default"}), 200

    if is_greeting:
        welcome = generate_welcome(is_returning=False, name=user_doc.get("name"))
        send_text(phone, clinic_id, welcome)
    send_main_menu(phone, clinic_id, mark_seen=True)
    return jsonify({"status": "fresh_welcome"}), 200
