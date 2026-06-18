"""
Simplified field extraction for appointment booking — Sarvam-30B.
Extracts structured fields from natural speech. Used as a silent background
step; the LLM generates all user-facing responses.
"""
import json
import logging
from datetime import datetime, timedelta

from services.llm import extract_json

logger = logging.getLogger("extraction")

# ── Base prompt: schema + rules that sarvam-30b won't infer on its own ────────
# Intentionally short — sarvam-30b is a native multilingual model that understands
# affirmative/negative in all Indian languages. No need to enumerate word lists.
EXTRACTION_PROMPT = r"""Extract appointment booking fields from the user's speech. Output ONLY valid JSON — no markdown, no explanation.

JSON schema: { "intent", "name", "age", "department", "date", "time", "confirm", "doctor", "reason", "contact_number", "preferred_channel", "email_address" }

CRITICAL — FIELD ISOLATION:
Extract ONLY what the user says in the CURRENT turn. Do NOT copy from "Booking draft".
If the user gives just a name → only name, rest null. Just a number → only age, rest null. Just yes/no → only confirm, rest null.

intent: "book" | "reschedule" | "cancel" | "none"
  - "book": any new appointment request in any language.
  - "reschedule": only if user explicitly wants to change an existing booked appointment.
  - "cancel": only if user explicitly says cancel/nahi chahiye/band karo. NOT for farewells or "no thank you".
  - "none": greetings, clarifications, farewells, unclear speech.

name: string or null
  - A proper person name only. Null if speech is a fragment without a name.
  - NEVER return family relationship words as names. These words describe who the patient is, not their actual name:
    mother/mom/mum/mummy/মা/আম্মা/মায়ের/माँ/मां/मा/माता/அம்மா/అమ్మ/ಅಮ್ಮ/അമ്മ/
    father/dad/papa/বাবা/বাপ/पिता/पापा/अब्बा/அப்பா/నాన్న/ಅಪ್ಪ/അച്ഛൻ/
    wife/husband/spouse/brother/sister/son/daughter/friend/relative/দিদি/দাদা/ভাই/বোন/भाई/बहन
    → return null for name; the system will ask for the patient's actual full name.

age: digit string "1"-"120" or null
  - Numeric ages only. Convert spoken words (twenty-nine → "29"), regional digits (२९ → "29").
  - Never extract age from time values, prices, slot numbers, or any non-age number.

department: string or null
  - Set ONLY when user explicitly names a body part, symptom, or medical specialty.
  - NEVER infer from visit type: "recheck", "follow-up", "checkup", "doctor said come again",
    "routine visit", "same doctor" → always null. The user must say it.
  - Symptom mapping (only when user describes a complaint): tooth/dental→"Dental",
    skin/rash→"Skin/Dermatology", knee/joint/bone→"Ortho", ear/nose/throat→"ENT",
    eye→"Eye/Ophthalmology", stomach/digestion→"Gastroenterology".
  - If clinic departments are listed below, match to the closest one in that list.

date: "DD MMM" resolved date (e.g. "18 Jun", "19 Jun", "20 Jun") or null
  - ALWAYS resolve relative references to an actual date using TODAY's date provided below.
  - "today" → today's date. 
  - "tomorrow" / "kal" / "আগামীকাল" / "நாளை" / "రేపు" → today + 1 day.
  - "day after tomorrow" / "परसों" / "পরশু" / "ఎల్లుండి" / "நாளை மறுநாள்" → today + 2 days.
  - "next week" → today + 7 days.
  - Weekday names (monday/tuesday/etc.) → the next upcoming occurrence of that day.
  - NEVER return the words "today", "tomorrow", "day after tomorrow" — always output "DD MMM".
  - When the appointment is wanted, not how long they've had a symptom.
  - "X days ago / since X days / for X days" → null (symptom duration, not appointment date).

time: "HH:MM AM/PM" | "morning" | "afternoon" | "evening" | null
  - Never extract time from age values.
  - Vague preference words (subah/shaam/morning/evening) → "morning"/"evening" (not a specific hour).
  - If slot options are listed below, map user's selection to the exact slot string.

confirm: true | false | null
  - true: any clear yes/agreement in any language (हाँ, yes, okay, சரி, ಸರಿ, ആണ്, etc.)
  - false: any clear no/rejection in any language (नहीं, no, nope, இல்லை, ಇಲ್ಲ, etc.)
  - null: user gave information but did not confirm or deny anything

doctor: string or null
  - The preferred doctor's name if the user mentions one (e.g. "Dr. Sharma", "Doctor Gupta").
  - null if no specific doctor is mentioned. NEVER infer a doctor from department name.

reason: string or null
  - The patient's chief complaint or reason for visit in ≤6 words (e.g. "knee pain", "fever", "routine checkup").
  - Extract ONLY when user explicitly describes a health complaint or visit reason.
  - Do NOT infer reason from department alone.

contact_number: string or null
  - An alternate phone number the user explicitly provides for confirmation (e.g. "+919876543210").
  - null if user says "same number" / "this number" / does not provide a new one.

preferred_channel: "whatsapp" | "sms" | "email" | "voice" | null
  - How the user wants to receive appointment confirmation.
  - "whatsapp" if user says WhatsApp / WA. "sms" if user says SMS / text. "email" if user says email. "voice" if user says call me.
  - null if not specified.

email_address: string or null
  - An email address if the user provides one (e.g. "john@example.com").
  - null if no email is mentioned.
"""


async def extract_booking_info(
    user_text: str,
    current_draft: dict | None = None,
    clinic_departments: list[str] | None = None,
    recent_utterances: list[str] | None = None,
    offered_times: list[str] | None = None,
    awaiting_state: str | None = None,
    today_date: str | None = None,
) -> dict:
    """
    Extract booking fields from user's natural language input.

    Args:
        user_text: the current user utterance
        current_draft: collected fields so far (shown to model as context, NOT to copy)
        clinic_departments: list of valid departments at this clinic
        recent_utterances: prior utterance fragments (for barge-in stitching)
        offered_times: slots previously offered, so user selections resolve correctly
        awaiting_state: hint about what the flow is waiting for, e.g.
            "awaiting age confirmation", "awaiting slot selection", "collecting department"
    Returns dict with: intent, name, age, department, date, time, confirm, doctor, reason, contact_number, preferred_channel, email_address
    """
    system_prompt = EXTRACTION_PROMPT

    # ── Inject today's date so model can resolve relative dates ──────────────
    _today = today_date or datetime.now().strftime("%-d %b")
    system_prompt = f"TODAY'S DATE: {_today}\n\n" + system_prompt

    # ── State hint: tell the model what to focus on ───────────────────────────
    # This is the key improvement: instead of one generic prompt trying to handle
    # every scenario, we inject a one-line focus hint per turn.
    if awaiting_state:
        system_prompt += f"\n\nCURRENT STEP: {awaiting_state}\nFocus extraction on what this step needs."

    # ── Slot mapping: only when slots were offered ────────────────────────────
    if offered_times:
        lines = []
        for slot in offered_times:
            hour_part = slot.split(":")[0].lstrip("0") or "0"
            period = slot.split(" ")[1] if " " in slot else ""
            lines.append(
                f'  "{slot}" — user may say "{hour_part}", "{hour_part} {period}", '
                f'or spoken form (e.g. "six", "five thirty")'
            )
        system_prompt += (
            "\n\nAVAILABLE SLOTS (map user's selection to exact string):\n"
            + "\n".join(lines)
        )

    # ── Department list: only when relevant ──────────────────────────────────
    if clinic_departments:
        system_prompt += (
            f"\n\nCLINIC DEPARTMENTS: {', '.join(clinic_departments)}\n"
            f"Match user's symptom/specialty to the closest name in this list. "
            f"If not in the list, return the spoken specialty anyway."
        )

    # ── Build user prompt ─────────────────────────────────────────────────────
    draft_text = json.dumps(current_draft or {}, ensure_ascii=False)

    recent_ctx = ""
    if recent_utterances:
        fragments = " | ".join(u.strip() for u in recent_utterances if u.strip())
        if fragments:
            recent_ctx = f"Recent speech (for context only): {fragments}\n"

    user_prompt = (
        f"Booking draft (DO NOT copy, context only): {draft_text}\n"
        f"{recent_ctx}"
        f'User said: "{user_text}"\n'
        f"Extract JSON:"
    )

    try:
        result = await extract_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_s=8.0,
        )
        if not result:
            logger.warning(f"Extraction returned empty for: '{user_text}'")
            return {"intent": "none"}

        # ── Post-process age: normalise regional digits, validate range ───────
        age = result.get("age")
        if age is not None:
            try:
                age_str = str(age).strip().split()[0]
                age_str = age_str.translate(str.maketrans(
                    "०१२३४५६७८९"   # Devanagari
                    "০১২৩৪৫৬৭৮৯"   # Bengali
                    "੦੧੨੩੪੫੬੭੮੯"   # Gurmukhi
                    "૦૧૨૩૪૫૬૭૮૯"   # Gujarati
                    "೦೧೨೩೪೫೬೭೮೯"   # Kannada
                    "൦൧൨൩൪൫൬൭൮൯"   # Malayalam
                    "௦௧௨௩௪௫௬௭௮௯"   # Tamil
                    "౦౧౨౩౪౫౬౭౮౯",  # Telugu
                    "0123456789" * 8
                ))
                age_num = int(age_str)
                if not (1 <= age_num <= 120):
                    raise ValueError("out of range")
                result["age"] = str(age_num)
            except (ValueError, AttributeError):
                logger.warning(f"Rejecting invalid age: '{age}'")
                result["age"] = None

        logger.info(f"Extracted: {result}")

        # ── Post-process date: resolve any remaining relative strings ─────────
        # Safety net in case the model returns "tomorrow" despite instructions.
        raw_date = result.get("date")
        if raw_date:
            _now = datetime.now()
            _rel = raw_date.lower().strip()
            _resolved = None
            if _rel in ("today", "aaj", "आज", "আজ"):
                _resolved = _now.strftime("%-d %b")
            elif _rel in ("tomorrow", "kal", "कल", "আগামীকাল", "नाल", "வெளே"):
                _resolved = (_now + timedelta(days=1)).strftime("%-d %b")
            elif _rel in ("day after tomorrow", "परसों", "परसो", "परवा", "পরশু", "পরশুদিন", "ఎల్లుండి", "நாளை மறுநாள்"):
                _resolved = (_now + timedelta(days=2)).strftime("%-d %b")
            elif _rel in ("next week",):
                _resolved = (_now + timedelta(days=7)).strftime("%-d %b")
            else:
                # Weekday resolution
                _weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                             "friday": 4, "saturday": 5, "sunday": 6}
                for _day, _idx in _weekdays.items():
                    if _day in _rel:
                        _delta = (_idx - _now.weekday()) % 7 or 7
                        _resolved = (_now + timedelta(days=_delta)).strftime("%-d %b")
                        break
            if _resolved:
                logger.info(f"[Date resolve] '{raw_date}' → '{_resolved}'")
                result["date"] = _resolved

        return result

    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return {"intent": "none"}
