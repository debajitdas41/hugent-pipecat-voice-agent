"""
Smart Context Builder — clean English-only context injection.

Drop-in replacement for services/context.py's build_booking_context().
Key differences from the old version:
  - No per-language instruction dictionaries (removed ~400 lines of native translations)
  - sarvam-30b understands plain English instructions and responds in whatever
    language the conversation is in — no need to repeat rules in 11 languages
  - Context blocks are shorter and more signal-like: state facts, let LLM reason

The old services/context.py is kept for reference. Switch manager.py's import
from `services.context` to `services.smart_context` to activate this.
"""
from typing import Optional

import logging
logger = logging.getLogger("smart_context")


def build_smart_context(
    draft: dict,
    missing_fields: list[str],
    slot_info: Optional[dict] = None,
    booking_result: Optional[dict] = None,
    existing_appointment: Optional[dict] = None,
    name_to_confirm: Optional[str] = None,
    age_to_confirm: Optional[str] = None,
    original_name: Optional[str] = None,
    awaiting_confirmation: bool = False,
    awaiting_reschedule_confirm: bool = False,
    time_clarification: Optional[str] = None,
    slot_options: Optional[list[str]] = None,
    department_unavailable: Optional[str] = None,
    clinic_departments: Optional[list[str]] = None,
    user_wants_change: bool = False,
    validation_failed: bool = False,
    lang: str = "en",
) -> str:
    """
    Build a context block to append to the system prompt.

    Each block describes the current state as plain English facts.
    The LLM reads the state and responds naturally in whatever language
    the conversation is in — no per-language instruction needed here.

    Returns empty string if nothing is in progress.
    """

    # ── Validation failure: tried to confirm but fields missing ──────────────
    if validation_failed and missing_fields:
        missing_str = ", ".join(missing_fields)
        return (
            f"[Validation Failed]\n"
            f"Cannot confirm — these fields are still missing: {missing_str}.\n"
            f"Apologise briefly and ask for the first missing field only: {missing_fields[0]}."
        )

    # ── Department not available at this clinic ───────────────────────────────
    if department_unavailable:
        if clinic_departments:
            dept_list = ", ".join(clinic_departments)
            return (
                f"[Department Not Available]\n"
                f"The caller's concern maps to '{department_unavailable}', "
                f"but this clinic does not offer that specialty.\n"
                f"Available departments: {dept_list}.\n"
                f"Apologise briefly, tell them which departments are available, "
                f"and help them pick the closest match for their problem."
            )
        return (
            f"[Department Not Available]\n"
            f"The caller asked for '{department_unavailable}' but this clinic doesn't offer it.\n"
            f"Let them know and ask if they have a different health concern we can help with."
        )

    # ── Existing appointment found (reschedule prompt) ────────────────────────
    if existing_appointment and awaiting_reschedule_confirm:
        apt_date = existing_appointment.get("date", "unknown date")
        apt_time = existing_appointment.get("time", "unknown time")
        return (
            f"[Existing Appointment]\n"
            f"Caller already has an appointment on {apt_date} at {apt_time}.\n"
            f"Ask naturally: would they like to reschedule it or book a new one?"
        )

    # ── Name confirmation ─────────────────────────────────────────────────────
    if name_to_confirm:
        # Issue 9: show already-collected fields so LLM never re-asks them.
        _nc_other = ", ".join(
            f"{k}={v}" for k, v in draft.items() if v and k != "name"
        ) if draft else ""
        _nc_note = f"\nAlready collected (do NOT ask for these again): {_nc_other}." if _nc_other else ""
        if original_name and original_name != name_to_confirm:
            return (
                f"[Name Confirmation]{_nc_note}\n"
                f"You heard '{original_name}' and understood it as '{name_to_confirm}'.\n"
                f"Confirm naturally: is their name {name_to_confirm}?"
            )
        return (
            f"[Name Confirmation]{_nc_note}\n"
            f"You heard the patient's name as '{name_to_confirm}'.\n"
            f"Confirm naturally: is their name {name_to_confirm}?"
        )

    # ── Age confirmation ──────────────────────────────────────────────────────
    if age_to_confirm:
        # Issue 9: show already-collected fields so LLM never re-asks them.
        _ac_other = ", ".join(
            f"{k}={v}" for k, v in draft.items() if v and k != "age"
        ) if draft else ""
        _ac_note = f"\nAlready collected (do NOT ask for these again): {_ac_other}." if _ac_other else ""
        return (
            f"[Age Confirmation]{_ac_note}\n"
            f"You heard the patient's age as {age_to_confirm}.\n"
            f"Confirm naturally: is their age {age_to_confirm}?"
        )

    # ── Time clarification (AM/PM ambiguity) ──────────────────────────────────
    if time_clarification:
        return (
            f"[Time Clarification]\n"
            f"The caller said '{time_clarification}' — morning or evening is ambiguous.\n"
            f"Ask naturally: do they mean morning or evening?"
        )

    # ── Available slots to choose from ───────────────────────────────────────
    if slot_options:
        options_str = ", ".join(slot_options)
        d = draft
        name = d.get("name", "")
        age = d.get("age", "")
        dept = d.get("department", "")
        date = d.get("date", "")
        preferred_period = slot_info.get("preferred_period", "") if isinstance(slot_info, dict) else ""
        period_note = f" (filtered to {preferred_period} as requested)" if preferred_period else ""

        return (
            f"[Available Slots]\n"
            f"Booking status — collected: name={name}, age={age}, "
            f"dept={dept}, date={date}. Waiting for: time only. NOT BOOKED YET.\n"
            f"Available on {date}{period_note}: {options_str}.\n"
            f"Present the options naturally and ask which one works for them.\n"
            f"When they pick a time, say only a brief acknowledgment (e.g. 'Perfect!') and stop. "
            f"Do NOT say the appointment is booked. The system will handle confirmation next."
        )

    # ── All fields collected — asking for confirmation ────────────────────────
    if awaiting_confirmation and not missing_fields:
        d = draft
        name = d.get("name", "")
        age = d.get("age", "")
        dept = d.get("department", "")
        date = d.get("date", "")
        time = d.get("time", "")

        if not all([name, age, dept, date, time]):
            still_missing = [f for f in ["name", "age", "department", "date", "time"] if not d.get(f)]
            return (
                f"[Validation Failed]\n"
                f"Cannot confirm — still missing: {', '.join(still_missing)}.\n"
                f"Ask for: {still_missing[0]}."
            )

        return (
            f"[Confirm Appointment]\n"
            f"All fields collected. The appointment is NOT booked yet.\n"
            f"Read back these exact details naturally, then ask 'Shall I go ahead and book this?':\n"
            f"  Patient: {name}, Age: {age}, Department: {dept}, Date: {date}, Time: {time}\n"
            f"Use these values exactly — do not substitute from conversation history.\n"
            f"Wait for their yes/no before doing anything else.\n"
            f"Do NOT say it is booked, confirmed, or scheduled yet."
        )

    # ── Booking result ────────────────────────────────────────────────────────
    if booking_result:
        if booking_result.get("success"):
            d = draft
            return (
                f"[Booking Confirmed]\n"
                f"Appointment successfully booked:\n"
                f"  Patient: {d.get('name')}, Age: {d.get('age')}, "
                f"Department: {d.get('department')}, "
                f"Date: {d.get('date')}, Time: {d.get('time')}\n"
                f"Respond warmly in natural conversation — congratulate them, confirm the date and time, "
                f"and mention they should arrive 15 minutes early for registration.\n"
                f"Do NOT output '[Booking Confirmed]' verbatim. Vary the wording naturally."
            )
        if booking_result.get("slot_full"):
            available = booking_result.get("available_times", [])
            nearest = booking_result.get("nearest_time")
            if nearest:
                return (
                    f"[Slot Unavailable]\n"
                    f"The requested time slot is full.\n"
                    f"Nearest available: {nearest}. Suggest it naturally and ask if it works."
                )
            if available:
                options = ", ".join(available[:5])
                return (
                    f"[Slot Unavailable]\n"
                    f"The requested time slot is full.\n"
                    f"Other available times: {options}. Offer them naturally."
                )
            return (
                f"[No Slots Available]\n"
                f"No slots available on the requested date.\n"
                f"Ask naturally if they'd like to try a different date."
            )
        if booking_result.get("error"):
            return (
                f"[Booking Error]\n"
                f"Something went wrong while booking. Apologise briefly and ask if they'd like to try again."
            )

    # ── User rejected confirmation — wants to change something ────────────────
    if user_wants_change:
        d = draft
        return (
            f"[Change Request]\n"
            f"The caller said no to the confirmation.\n"
            f"Current details: name={d.get('name')}, age={d.get('age')}, "
            f"dept={d.get('department')}, date={d.get('date')}, time={d.get('time')}.\n"
            f"Ask which detail they'd like to change — name, age, department, date, or time. "
            f"One question, wait for their answer."
        )

    # ── Active booking in progress ────────────────────────────────────────────
    collected = {k: v for k, v in draft.items() if v}
    if not collected and not missing_fields:
        return ""  # Nothing in progress

    collected_str = ", ".join(f"{k}={v}" for k, v in collected.items()) if collected else "none yet"
    missing_str = ", ".join(missing_fields) if missing_fields else "none — ready to confirm"
    next_field = missing_fields[0] if missing_fields else None

    next_instruction = (
        f"Ask for '{next_field}' next — one question only."
        if next_field
        else "All fields collected. Move to confirmation."
    )

    return (
        f"[Booking In Progress — NOT BOOKED YET]\n"
        f"Collected so far: {collected_str}\n"
        f"Still needed: {missing_str}\n"
        f"{next_instruction}\n"
        f"Do NOT say the appointment is booked, confirmed, or scheduled. "
        f"Do NOT summarise all details until [Confirm Appointment] is shown."
    )
