"""
Voice Booking Manager v2 — Thin state tracker.

Key difference from V1: NO response generation. The LLM generates all
user-facing text. This module only:
  1. Tracks booking draft state (fields collected)
  2. Parses dates/times from extracted text
  3. Calls Backend_saas for slot checks + reservations
  4. Returns context dicts for the LLM to use

The context.py module converts the state into LLM-friendly context strings.
"""
import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher

from services.extraction import extract_booking_info
from services.backend_client import BackendClient
from services.name_correction import correct_indian_name
from services.context import build_booking_context  # kept for reference / rollback
from services.smart_context import build_smart_context as build_booking_context  # noqa: F811 — active implementation

logger = logging.getLogger("booking")

FIELD_ORDER = ["name", "department", "date", "time", "reason"]
# Optional fields — collected opportunistically if offered; never asked explicitly
OPTIONAL_FIELDS = ["doctor", "age"]


# ──────────────────────────────────────────────────────────────
#  Date / Time parsing helpers  (kept from V1 — pure logic)
# ──────────────────────────────────────────────────────────────
def _format_date_label(dt: datetime) -> str:
    day = dt.strftime("%d").lstrip("0")
    return f"{day} {dt.strftime('%b')}"


def _parse_date_text(text: str, now: datetime) -> Optional[str]:
    raw = text.lower().strip()
    if not raw:
        return None
    if "today" in raw:
        return _format_date_label(now)
    if "tomorrow" in raw:
        return _format_date_label(now + timedelta(days=1))
    
    # ✅ Day after tomorrow (English + multilingual)
    day_after_tomorrow_phrases = [
        "day after tomorrow", "overmorrow",
        # Hindi
        "परसों", "parso", "parson", "परसो", "day after tom",
        # Bengali
        "পরশু", "পরশুদিন",
        # Tamil
        "நாளை மறுநாள்",
        # Telugu
        "ఎల్లుండి",
        # Kannada
        "ನಾಳಿದ್ದು",
        # Malayalam
        "മറ്റന്നാൾ",
        # Gujarati
        "પરમ દિવસે",
        # Marathi
        "परवा",
        # Punjabi
        "ਪਰਸੋਂ",
        # Odia
        "ପରଶୁଦିନ",
    ]
    for phrase in day_after_tomorrow_phrases:
        if phrase in raw or phrase in text:
            return _format_date_label(now + timedelta(days=2))

    # ── Week/month relative phrases (English + multilingual) ──
    week_phrases = [
        "next week", "week later", "a week later", "one week later",
        "in a week", "after a week", "week after", "in one week",
        # Hindi
        "अगले हफ्ते", "एक हफ्ते बाद", "हफ्ते बाद", "अगले सप्ताह", "सप्ताह बाद",
        # Bengali
        "সপ্তাহ পর", "পরের সপ্তাহ", "এক সপ্তাহ পর", "আগামী সপ্তাহ",
        # Tamil/Telugu/Kannada/Malayalam/Gujarati/Marathi/Punjabi/Odia
        "அடுத்த வாரம்", "ఒక వారం తర్వాత", "ಮುಂದಿನ ವಾರ", "അടുത്ത ആഴ്ച",
        "આવતા અઠવાડિયે", "पुढच्या आठवड्यात", "ਅਗਲੇ ਹਫ਼ਤੇ", "ଆଗାମୀ ସପ୍ତାହ",
    ]
    month_phrases = [
        "next month", "a month later", "one month later", "month later",
        # Hindi
        "अगले महीने", "महीने बाद",
        # Bengali
        "পরের মাস", "মাস পর", "আগামী মাস",
    ]
    for phrase in week_phrases:
        if phrase in raw or phrase in text.lower():
            return _format_date_label(now + timedelta(days=7))
    for phrase in month_phrases:
        if phrase in raw or phrase in text.lower():
            return _format_date_label(now + timedelta(days=30))

    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    for day_name, target_idx in weekdays.items():
        if day_name in raw:
            delta = (target_idx - now.weekday()) % 7
            if delta == 0:
                delta = 7
            return _format_date_label(now + timedelta(days=delta))
    return None


# ── Department name normalization (multilingual → English canonical) ──
_DEPT_MAP: dict[str, str] = {
    # English variants (lowercase)
    "general": "General", "general medicine": "General", "medicine": "General",
    "doctor": "General", "physician": "General", "gp": "General",
    "dental": "Dental", "dentist": "Dental", "teeth": "Dental", "tooth": "Dental",
    "dermatology": "Dermatology", "dermat": "Dermatology", "skin": "Dermatology",
    "orthopedic": "Orthopedic", "orthopaedic": "Orthopedic", "orthopedics": "Orthopedic",
    "ortho": "Orthopedic", "bone": "Orthopedic", "joints": "Orthopedic",
    "pediatric": "Pediatric", "paediatric": "Pediatric", "pediatrics": "Pediatric",
    "child": "Pediatric", "children": "Pediatric", "kids": "Pediatric",
    "chest": "Chest", "pulmonology": "Chest", "respiratory": "Chest",
    "ent": "ENT", "ear nose throat": "ENT", "ear": "ENT", "nose": "ENT", "throat": "ENT",
    "screen": "ENT", "hearing": "ENT",   # common STT mishearing for ear
    "cardiology": "Cardiology", "heart": "Cardiology",
    "neurology": "Neurology", "neuro": "Neurology",
    "gynecology": "Gynecology", "gynaecology": "Gynecology", "gynae": "Gynecology",
    "ophthalmology": "Ophthalmology", "eye": "Ophthalmology", "eyes": "Ophthalmology",
    "gastroenterology": "General", "gastro": "General", "stomach": "General",
    "gastrology": "General",
    # Hindi
    "सामान्य": "General", "जनरल": "General", "सामान्य चिकित्सा": "General",
    "डॉक्टर": "General", "डॉक्टर दिखाना": "General", "डॉक्टर से मिलना": "General",
    "पेट": "General", "पेट दर्द": "General", "पेट में दर्द": "General",
    "दंत": "Dental", "दांत": "Dental", "डेंटल": "Dental",
    "त्वचा": "Dermatology", "त्वचा रोग": "Dermatology", "डर्मेटोलॉजी": "Dermatology",
    "आंख": "Ophthalmology", "नेत्र": "Ophthalmology",
    "ऑर्थोपेडिक": "Orthopedic", "हड्डी": "Orthopedic", "ऑर्थो": "Orthopedic",
    "घुटना": "Orthopedic", "अस्थि": "Orthopedic", "जोड़": "Orthopedic",
    "बाल रोग": "Pediatric", "बच्चे": "Pediatric", "पीडियाट्रिक": "Pediatric",
    "छाती": "Chest", "फेफड़े": "Chest", "सांस": "Chest",
    "हृदय": "Cardiology", "दिल": "Cardiology",
    "कान नाक गला": "ENT", "कान": "ENT", "नाक": "ENT", "गला": "ENT",
    "स्क्रीन": "ENT",  # common STT mishearing of श्रवण/सुनना for ENT problems
    # Bengali
    "সাধারণ": "General", "জেনারেল": "General", "সাধারণ চিকিৎসা": "General",
    "দাঁত": "Dental", "ডেন্টাল": "Dental", "দন্ত": "Dental",
    "চর্ম": "Dermatology", "ডার্মাটোলজি": "Dermatology", "চামড়া": "Dermatology",
    "অর্থোপেডিক": "Orthopedic", "অর্থো": "Orthopedic", "অর্থ": "Orthopedic", "হাড়": "Orthopedic", "জয়েন্ট": "Orthopedic",
    "শিশু": "Pediatric", "শিশুরোগ": "Pediatric", "বাচ্চা": "Pediatric",
    "বুক": "Chest", "ফুসফুস": "Chest", "শ্বাস": "Chest",
    "হৃদয়": "Cardiology", "হার্ট": "Cardiology",
    "চোখ": "Ophthalmology", "চক্ষু": "Ophthalmology",
    "কান নাক গলা": "ENT", "কান": "ENT", "নাক": "ENT", "গলা": "ENT",
    # Tamil
    "பொது": "General", "பல்": "Dental", "தோல்": "Dermatology",
    "எலும்பு": "Orthopedic", "குழந்தை": "Pediatric", "மார்பு": "Chest",
    # Telugu
    "సాధారణ": "General", "దంత": "Dental", "చర్మ": "Dermatology",
    "ఎముక": "Orthopedic", "పిల్లల": "Pediatric", "ఛాతీ": "Chest",
    # Kannada
    "ಸಾಮಾನ್ಯ": "General", "ಹಲ್ಲು": "Dental", "ಚರ್ಮ": "Dermatology",
    "ಮೂಳೆ": "Orthopedic", "ಮಕ್ಕಳು": "Pediatric", "ಎದೆ": "Chest",
    # Malayalam
    "ദന്ത": "Dental", "ത്വക്ക്": "Dermatology", "അസ്ഥി": "Orthopedic",
    "ശിശു": "Pediatric", "നെഞ്ച്": "Chest",
    # Gujarati
    "સામાન્ય": "General", "દાંત": "Dental", "ત્વચા": "Dermatology",
    "હાડકા": "Orthopedic", "બાળ": "Pediatric", "છાતી": "Chest",
    # Marathi
    "सामान्य": "General", "दात": "Dental", "त्वचा": "Dermatology",
    "हाड": "Orthopedic", "बाल": "Pediatric", "छाती": "Chest",
    # Punjabi
    "ਆਮ": "General", "ਦੰਦ": "Dental", "ਚਮੜੀ": "Dermatology",
    "ਹੱਡੀ": "Orthopedic", "ਬੱਚੇ": "Pediatric", "ਛਾਤੀ": "Chest",
}


def _normalize_department(value: Optional[str]) -> Optional[str]:
    """Map multilingual or lowercase department name to English canonical form."""
    if not value:
        return value
    stripped = value.strip()
    key = stripped.lower()
    # Direct lookup (lowercase)
    if key in _DEPT_MAP:
        return _DEPT_MAP[key]
    # Unicode/non-ASCII lookup (exact match)
    if stripped in _DEPT_MAP:
        return _DEPT_MAP[stripped]
    # Partial match: check if any map key is contained in the value
    for k, v in _DEPT_MAP.items():
        if k in key or k in stripped:
            return v
    # Return title-cased original if nothing matched (best effort)
    return stripped.title()


# ── Relationship words: never valid as a patient name ─────────────────────────
# When extraction returns one of these, the system must ask for the actual name.
_RELATIONSHIP_WORDS: frozenset[str] = frozenset({
    # English
    "mother", "mom", "mum", "mama", "father", "dad", "papa",
    "wife", "husband", "spouse", "brother", "sister", "son", "daughter",
    "aunt", "uncle", "grandmother", "grandfather", "grandma", "grandpa",
    "friend", "relative", "patient",
    # Hindi
    "माँ", "मां", "मा", "माता", "पिता", "पापा", "बाबा", "पत्नी", "पति",
    "भाई", "बहन", "बेटा", "बेटी", "दादी", "दादा", "नानी", "नाना",
    # Bengali
    "মা", "মাম", "বাবা", "স্ত্রী", "স্বামী", "ভাই", "বোন",
    "ছেলে", "মেয়ে", "দাদা", "দিদি", "মায়ের",
    # Tamil
    "அம்மா", "அப்பா", "மனைவி", "கணவன்",
    # Telugu
    "అమ్మ", "నాన్న", "భార్య", "భర్త",
    # Kannada
    "ಅಮ್ಮ", "ಅಪ್ಪ", "ಹೆಂಡತಿ", "ಗಂಡ",
    # Malayalam
    "അമ്മ", "അച്ഛൻ", "ഭാര്യ", "ഭർത്താവ്",
    # Marathi
    "आई",
})

# Build reverse map: canonical category → all synonym keywords.
# Used to fuzzy-match a canonical name against a clinic's actual dept list.
_CANONICAL_SYNONYMS: dict[str, set[str]] = {}
for _syn, _canon in _DEPT_MAP.items():
    _CANONICAL_SYNONYMS.setdefault(_canon, set()).add(_syn.lower())
    _CANONICAL_SYNONYMS[_canon].add(_canon.lower())


def _similarity_ratio(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings (0.0 to 1.0)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _phonetic_match(a: str, b: str) -> bool:
    """Check if two words sound similar (simple phonetic matching).
    
    Handles common STT mishearings:
    - "Mental" → "Dental" (m/d confusion)
    - "Gental" → "Dental" (g/d confusion)
    - "Pental" → "Dental" (p/d confusion)
    """
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()
    
    # If they differ only in the first consonant and rest is same, consider them phonetically similar
    if len(a_lower) == len(b_lower) and len(a_lower) > 3:
        # Check if everything except first letter matches
        if a_lower[1:] == b_lower[1:]:
            # Common consonant confusions: m/d, g/d, p/d, b/p, t/d
            confusion_pairs = [
                ('m', 'd'), ('g', 'd'), ('p', 'd'), ('b', 'p'), ('t', 'd'),
                ('n', 'm'), ('l', 'r'), ('v', 'w'), ('f', 'v')
            ]
            first_a, first_b = a_lower[0], b_lower[0]
            for x, y in confusion_pairs:
                if (first_a == x and first_b == y) or (first_a == y and first_b == x):
                    return True
    
    return False


def _match_to_clinic_departments(canonical: Optional[str], clinic_departments: list[str]) -> Optional[str]:
    """Find the best-matching department from the clinic's actual department list.

    The extraction LLM returns a canonical category (e.g. 'Chest', 'Orthopedic').
    This function maps that to whatever the clinic actually named the department
    (e.g. 'Pulmonology', 'Chest Medicine', 'Bone & Joint').

    Matching priority:
      1. Exact case-insensitive match
      2. Clinic dept is a known synonym for the canonical category
      3. Any synonym for the canonical appears inside the clinic dept name
      4. The canonical name appears inside the clinic dept name
      5. ✅ PHONETIC MATCH: Sound-alike detection (e.g., "Mental" → "Dental", "Gental" → "Dental")
      6. ✅ FUZZY MATCH: Spelling similarity >= 0.75 using Levenshtein distance
    Falls back to the canonical as-is if no match found.
    """
    if not canonical or not clinic_departments:
        return canonical
    synonyms = _CANONICAL_SYNONYMS.get(canonical, {canonical.lower()})
    canonical_lower = canonical.lower()
    
    # Track best fuzzy match in case exact/synonym/substring fail
    best_fuzzy_match = None
    best_fuzzy_score = 0.0
    
    for dept in clinic_departments:
        dept_lower = dept.strip().lower()
        
        # 1. Exact match
        if dept_lower == canonical_lower:
            logger.debug(f"[Dept match] Exact: '{canonical}' → '{dept}'")
            return dept
        
        # 2. Synonym match
        if dept_lower in synonyms:
            logger.debug(f"[Dept match] Synonym: '{canonical}' → '{dept}' (synonym set)")
            return dept
        
        # 3. Substring match (synonym in dept name)
        for syn in synonyms:
            if syn and len(syn) >= 3 and syn in dept_lower:
                logger.debug(f"[Dept match] Substring: '{canonical}' → '{dept}' (via synonym '{syn}')")
                return dept
        
        # 4. Canonical in dept name
        if canonical_lower in dept_lower:
            logger.debug(f"[Dept match] Contains: '{canonical}' → '{dept}'")
            return dept
        
        # 5. ✅ PHONETIC MATCH: Check for sound-alike words (e.g., "Mental" → "Dental")
        if _phonetic_match(canonical_lower, dept_lower):
            logger.info(f"[Dept match] Phonetic: '{canonical}' → '{dept}' (sound-alike)")
            return dept
        
        # 6. ✅ FUZZY MATCH: Track best similarity score
        similarity = _similarity_ratio(canonical_lower, dept_lower)
        if similarity > best_fuzzy_score:
            best_fuzzy_score = similarity
            best_fuzzy_match = dept
    
    # If fuzzy match is strong enough (>= 0.75), use it
    # This catches "Mental" → "Dental" (0.83 similarity)
    if best_fuzzy_score >= 0.75 and best_fuzzy_match:
        logger.info(
            f"[Dept match] Fuzzy: '{canonical}' → '{best_fuzzy_match}' "
            f"(similarity={best_fuzzy_score:.2f})"
        )
        return best_fuzzy_match
    
    # No match found — return canonical as-is
    logger.warning(
        f"[Dept match] No match: '{canonical}' not found in clinic departments {clinic_departments}. "
        f"Best fuzzy score: {best_fuzzy_score:.2f}"
    )
    return canonical


def _parse_time_text(text: str) -> Optional[str]:
    raw = text.lower()
    # Reject age-like context
    if any(kw in raw for kw in ["age", "year", "old", "years old", "my age", "i am", "i'm"]):
        return None
    time_kws = ["at", "around", "time", "clock", "appointment", "slot",
                 "before", "after", "morning", "evening", "afternoon"]
    if not any(kw in raw for kw in time_kws) and ":" not in raw and not re.search(r"(am|pm)", raw):
        return None
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)
    if not meridiem and hour > 23:
        return None
    if not meridiem:
        if any(w in raw for w in ["pm", "evening", "night", "afternoon"]):
            meridiem = "pm"
        elif any(w in raw for w in ["am", "morning"]):
            meridiem = "am"
    if meridiem:
        meridiem = meridiem.upper()
        if hour == 12:
            hour = 0
        if meridiem == "PM":
            hour += 12
    if hour > 23 or minute > 59:
        return None
    return datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p")


def _smart_time_inference(time_str: str, booking_date: str, now: datetime) -> tuple[Optional[str], bool]:
    """Returns (resolved_time, needs_clarification)."""
    if "AM" in time_str.upper() or "PM" in time_str.upper():
        return time_str, False
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", time_str)
    if not match:
        return time_str, False
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour >= 13:
        return time_str, False
    today_label = _format_date_label(now)
    if booking_date == today_label:
        requested_am = now.replace(hour=hour if hour != 12 else 0, minute=minute, second=0, microsecond=0)
        requested_pm = now.replace(hour=hour + 12 if hour != 12 else 12, minute=minute, second=0, microsecond=0)
        if now > requested_am:
            return f"{hour:02d}:{minute:02d} PM", False
        if now > requested_pm:
            return None, False
        return None, True
    if 1 <= hour <= 12:
        return None, True
    return time_str, False


def _is_valid_time_format(time_str: Optional[str]) -> bool:
    if not time_str:
        return False
    return bool(re.match(r"^\d{1,2}:\d{2}\s*(AM|PM)$", time_str.strip(), re.IGNORECASE))


# Hour ranges (24h) for vague time periods
_PERIOD_HOUR_RANGES: dict[str, tuple[int, int]] = {
    "morning":   (5,  12),   # 05:00–11:59
    "afternoon": (12, 17),   # 12:00–16:59
    "evening":   (17, 21),   # 17:00–20:59
    "night":     (21, 24),   # 21:00–23:59
}


def _filter_slots_by_period(times: list[str], period: str) -> list[str]:
    """Return times that fall within *period*; falls back to all times if none match."""
    hour_range = _PERIOD_HOUR_RANGES.get(period.lower())
    if not hour_range:
        return times
    filtered = []
    for t in times:
        try:
            dt = datetime.strptime(t.strip(), "%I:%M %p")
            h = dt.hour
            if hour_range[0] <= h < hour_range[1]:
                filtered.append(t)
        except Exception:
            continue
    return filtered if filtered else times  # fallback so we never return empty


def _normalize(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _missing_fields(draft: dict) -> list[str]:
    missing = []
    for f in FIELD_ORDER:
        val = _normalize(draft.get(f))
        if f == "time":
            if not _is_valid_time_format(val):
                missing.append(f)
        elif not val:
            missing.append(f)
    return missing


def _find_nearest_slot(requested_time: str, available_times: list[str]) -> Optional[str]:
    if not available_times:
        return None
    try:
        req_dt = datetime.strptime(requested_time, "%I:%M %p")
        req_min = req_dt.hour * 60 + req_dt.minute
        closest = None
        min_diff = float("inf")
        for t in available_times:
            slot_dt = datetime.strptime(t, "%I:%M %p")
            diff = abs((slot_dt.hour * 60 + slot_dt.minute) - req_min)
            if diff < min_diff:
                min_diff = diff
                closest = t
        return closest
    except Exception:
        return available_times[0] if available_times else None


# ──────────────────────────────────────────────────────────────
#  Booking session state
# ──────────────────────────────────────────────────────────────
@dataclass
class BookingSession:
    session_id: str
    clinic_id: str
    phone: str
    draft: dict = field(default_factory=dict)
    intent: str = "none"
    booking_started: bool = False
    awaiting_confirmation: bool = False
    awaiting_name_confirmation: bool = False
    awaiting_age_confirmation: bool = False
    name_confirmed: bool = False
    age_confirmed: bool = False
    original_name: Optional[str] = None
    awaiting_reschedule_confirm: bool = False
    checked_existing: bool = False
    existing_appointment: Optional[dict] = None
    awaiting_time_clarification: bool = False
    time_clarification_value: Optional[str] = None
    user_id: Optional[str] = None
    language: str = "en"
    confirmation_presented_to_llm: bool = False  # True after one guard pass; prevents filler/noise from triggering booking
    booking_complete: bool = False  # True after successful _reserve; locks session against re-booking
    # Post-booking contact confirmation
    awaiting_contact_confirmation: bool = False
    awaiting_channel_selection: bool = False
    contact_number: Optional[str] = None
    preferred_channel: Optional[str] = None  # "whatsapp"|"sms"|"email"|"voice"
    email_address: Optional[str] = None
    # Failure tracking for human transfer trigger
    reserve_fail_count: int = 0


# ──────────────────────────────────────────────────────────────
#  Main manager
# ──────────────────────────────────────────────────────────────
class VoiceBookingManager:
    """Thin state tracker — no response generation."""

    def __init__(self, backend_client: BackendClient):
        self.backend = backend_client
        self.sessions: dict[str, BookingSession] = {}
        self._clinic_depts_cache: dict[str, list[str]] = {}
        logger.info(f"[VoiceBookingManager] Created new instance, id={id(self)}")

    def _session(self, session_id: str, clinic_id: str, phone: str) -> BookingSession:
        if session_id not in self.sessions:
            logger.info(f"[_session] Creating NEW session for {session_id}, manager_id={id(self)}")
            self.sessions[session_id] = BookingSession(
                session_id=session_id, clinic_id=clinic_id, phone=phone
            )
        else:
            logger.info(f"[_session] Retrieving EXISTING session {session_id}, draft={self.sessions[session_id].draft}, manager_id={id(self)}")
        return self.sessions[session_id]

    @property
    def draft(self) -> dict:
        """Shortcut for the current session's draft (for extraction context)."""
        if self.sessions:
            return list(self.sessions.values())[-1].draft
        return {}

    async def update_draft_only(
        self,
        session_id: str,
        clinic_id: str,
        phone: str,
        extraction: dict,
        lang: str = "en",
    ) -> None:
        """
        Merge extracted fields into the booking draft WITHOUT triggering
        slot checks, name correction, or any backend API calls.

        Called on barge-in so the draft stays in sync even when the full
        process_turn() is skipped. The next complete turn will call
        process_turn() normally and pick up the accumulated state.
        """
        s = self._session(session_id, clinic_id, phone)
        s.language = lang
        tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
        now = datetime.now(ZoneInfo(tz))

        intent = extraction.get("intent", "none")
        if intent in ("book", "reschedule"):
            s.booking_started = True
            s.intent = intent

        # Fetch clinic departments for validation
        clinic_depts = await self._get_clinic_departments(s.clinic_id)

        for f in FIELD_ORDER:
            val = _normalize(extraction.get(f))
            if val:
                if f == "date":
                    resolved = _parse_date_text(val, now)
                    s.draft[f] = resolved or val
                elif f == "time":
                    if val.lower() in ("morning", "evening", "afternoon", "night"):
                        if not _is_valid_time_format(s.draft.get("time")):
                            s.draft[f] = val
                    else:
                        parsed = _parse_time_text(val) or val
                        s.draft[f] = parsed
                elif f == "department":
                    canonical = _normalize_department(val)
                    matched = _match_to_clinic_departments(canonical, clinic_depts)
                    # Validate: is the matched value actually in the clinic's department list?
                    if clinic_depts and matched and matched not in clinic_depts:
                        logger.warning(f"[Barge-in] Department '{matched}' not in clinic departments {clinic_depts}. Rejected.")
                        s.draft[f] = None
                    else:
                        s.draft[f] = matched
                else:
                    s.draft[f] = val
        # Optional fields — accumulate opportunistically
        for f in OPTIONAL_FIELDS:
            val = _normalize(extraction.get(f))
            if val:
                s.draft[f] = val

    async def process_turn(
        self,
        session_id: str,
        clinic_id: str,
        phone: str,
        user_text: str,
        extraction: dict,
        lang: str = "en",
    ) -> dict:
        """
        Process a user turn. Updates booking state and returns a dict:
          {
            "context": str,        # LLM context to inject (empty = no booking)
            "end_call": bool,      # True if booking complete, close session
            "action": str | None,  # "book_now" if ready to reserve
          }
        """
        # ✅ CRITICAL: Log entry state for debugging
        s = self._session(session_id, clinic_id, phone)

        # ── Guard: booking already done ───────────────────────────────────────
        if s.booking_complete:
            # Post-booking: handle contact confirmation and channel selection
            if s.awaiting_contact_confirmation or s.awaiting_channel_selection:
                if s.awaiting_contact_confirmation:
                    return await self._handle_contact_confirmation(s, extraction, lang=lang)
                return await self._handle_channel_selection(s, extraction, lang=lang)
            # All post-booking done — return confirmed context and end call
            logger.info(f"[process_turn] booking_complete=True — returning Booking Confirmed, skipping processing")
            ctx = build_booking_context(s.draft, [], booking_result={"success": True}, lang=lang)
            return {"context": ctx, "end_call": True, "action": None}

        logger.info(f"[process_turn ENTRY] session_id={session_id}, manager_id={id(self)}, session_obj_id={id(s)}")
        logger.info(f"[process_turn ENTRY] user_text='{user_text[:50]}', extraction={extraction}")
        logger.info(f"[process_turn ENTRY] draft BEFORE update: {s.draft}, name_confirmed={s.name_confirmed}, age_confirmed={s.age_confirmed}")

        # ── Human transfer detection ──────────────────────────────────────────
        if self._detect_transfer_need(user_text, s):
            ctx = (
                "[Transfer to Human]\n"
                "IMMEDIATELY say: 'I'll connect you with our receptionist. Please stay on the line.' "
                "Do not offer anything else. End the call gracefully after this sentence."
            )
            return {"context": ctx, "end_call": True, "action": "transfer"}

        s.language = lang
        tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
        now = datetime.now(ZoneInfo(tz))

        intent = extraction.get("intent", "none")

        # ── Detect if we're in or entering a booking flow ──
        # ✅ FIX: Once booking_started is True, stay in flow even if draft is empty.
        # This allows fields to accumulate across turns where extraction doesn't always
        # return intent="book" (e.g., user says just "29" for age).
        in_flow = (
            intent in ("book", "reschedule")
            or s.booking_started  # ✅ FIXED: Don't require draft to be non-empty
            or s.awaiting_confirmation
            or s.awaiting_name_confirmation
            or s.awaiting_age_confirmation  # ✅ ADDED: Stay in flow during age confirmation
            or s.awaiting_reschedule_confirm
            or s.awaiting_time_clarification
            or s.awaiting_contact_confirmation
            or s.awaiting_channel_selection
        )

        # ✅ FIX: Field-based entry — if extraction returned ANY booking field,
        # enter the flow immediately. This prevents draft reset when user provides
        # name/age/etc without saying "appointment" keyword.
        if not in_flow:
            has_booking_field = any(extraction.get(f) for f in ["name", "age", "department", "date", "time", "reason", "doctor"])
            if has_booking_field:
                s.booking_started = True
                in_flow = True
                logger.info(f"[Booking flow] Field-based entry triggered: {extraction}")

        # Keyword fallback: bare appointment words should start the flow even when
        # the extraction LLM returns intent=none for single-word utterances like
        # "अपॉइंटमेंट।" — it sometimes misses the intent for short isolated words.
        if not in_flow:
            _BOOKING_STARTERS = (
                # English (+ common STT mishearings)
                "appointment", "booking", "apointment", "apointmant",
                # Hindi (Devanagari)
                "अपॉइंटमेंट", "बुकिंग", "अपॉइन्टमेंट",
                # Bengali
                "অ্যাপয়েন্টমেন্ট", "বুকিং", "অ্যাপয়েন্ট",
                # Tamil
                "அப்பாயிண்ட்மெண்ட்", "புக்கிங்",
                # Telugu
                "అపాయింట్మెంట్", "బుకింగ్",
                # Kannada
                "ಅಪಾಯಿಂಟ್ಮೆಂಟ್", "ಬುಕಿಂಗ್",
                # Malayalam
                "അപ്പോയിന്റ്മെന്റ്", "ബുക്കിങ്",
                # Marathi (Devanagari — same script as Hindi)
                "बुकिंग",
                # Gujarati
                "એપોઈન્ટમેન્ટ", "બુકિંગ",
                # Punjabi (Gurmukhi)
                "ਅਪੌਇੰਟਮੈਂਟ", "ਬੁਕਿੰਗ",
                # Odia
                "ଅ୍ୟାପଏଣ୍ଟମେଣ୍ଟ", "ବୁକିଂ",
            )
            if any(kw in user_text.lower() for kw in _BOOKING_STARTERS):
                s.booking_started = True
                in_flow = True
                intent = "book"
                logger.info(f"[Booking flow] Keyword fallback triggered: '{user_text[:60]}'")

        if not in_flow:
            return {"context": "", "end_call": False, "action": None}

        # ── Handle explicit cancel (abort current booking session) ──────────
        if intent == "cancel":
            s.booking_started = False
            s.awaiting_confirmation = False
            s.awaiting_name_confirmation = False
            s.awaiting_age_confirmation = False
            s.awaiting_reschedule_confirm = False
            s.awaiting_time_clarification = False
            s.draft = {}
            ctx = (
                "[Booking Cancelled]\n"
                "The patient wants to stop the booking. "
                "Acknowledge naturally (e.g. 'Sure, no problem!') and ask if there's anything else you can help with."
            )
            return {"context": ctx, "end_call": False, "action": None}

        # Fetch clinic departments early — cached after first call, so ~0 overhead on repeat turns
        clinic_depts = await self._get_clinic_departments(s.clinic_id)

        if intent in ("book", "reschedule"):
            s.booking_started = True
            s.intent = intent

        # ── Check existing appointments (first time) ──
        if intent in ("book", "reschedule") and not s.checked_existing:
            s.checked_existing = True
            existing = await self._check_existing(clinic_id, phone, now)
            if existing:
                s.existing_appointment = existing
                s.awaiting_reschedule_confirm = True
                ctx = build_booking_context(
                    s.draft, _missing_fields(s.draft),
                    existing_appointment=existing,
                    awaiting_reschedule_confirm=True,
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}

        # ── Pre-update: resolve confirmed offered-time slot ──────────────────
        # If user says "yes" without specifying a time, accept the first offered
        # slot.  Must run BEFORE the draft update loop so the time is in the
        # draft when the loop validates it.  Also clears spurious confirms when
        # fields are still missing (so confirmation handlers below don't fire).
        if extraction.get("confirm") is True and not extraction.get("time"):
            missing = _missing_fields(s.draft)
            if missing and not s.awaiting_confirmation:
                logger.warning(f"[Spurious confirm] User confirmed but fields missing: {missing}. Ignoring confirm.")
                extraction["confirm"] = None
            else:
                offered_times = getattr(s, "last_offered_times", [])
                if offered_times and not _is_valid_time_format(s.draft.get("time")):
                    s.draft["time"] = offered_times[0]
                    logger.info(f"[Slot confirmation] User confirmed, accepting offered time: {offered_times[0]}")

        # ── Update draft with extracted fields ────────────────────────────────
        # This runs on EVERY turn before any confirmation handler so that fields
        # from the current utterance always accumulate regardless of what
        # confirmation state the session is in.
        logger.info(f"[Draft update] BEFORE loop: draft={s.draft}")
        dept_unavailable_ctx: str | None = None  # set if dept is invalid; returned AFTER the full loop
        for f in FIELD_ORDER:
            val = _normalize(extraction.get(f))
            logger.debug(f"[Draft update] Field={f}, val={val}, will_update={bool(val)}")
            if val:
                if f == "date":
                    resolved = _parse_date_text(val, now)
                    s.draft[f] = resolved or val
                elif f == "time":
                    if val.lower() in ("morning", "evening", "afternoon", "night"):
                        if not _is_valid_time_format(s.draft.get("time")):
                            s.draft[f] = val
                    else:
                        parsed = _parse_time_text(val) or val
                        # ── Auto-correct AM/PM against last_offered_times ──
                        offered = getattr(s, 'last_offered_times', [])
                        if offered and parsed not in offered:
                            swapped = parsed.replace('AM', 'PM') if 'AM' in parsed else parsed.replace('PM', 'AM')
                            if swapped in offered:
                                logger.info(f"[Time AM/PM correction] Auto-corrected '{parsed}' → '{swapped}' (matches offered slot)")
                                parsed = swapped
                        s.draft[f] = parsed
                elif f == "department":
                    canonical = _normalize_department(val)
                    matched = _match_to_clinic_departments(canonical, clinic_depts)
                    logger.info(f"[Department update] Extracted='{val}', Canonical='{canonical}', Matched='{matched}', Clinic depts={clinic_depts}")
                    if clinic_depts and matched and matched not in clinic_depts:
                        # Dept not at this clinic — flag it but keep looping so
                        # date/time from the same utterance still land in draft.
                        logger.warning(f"Department '{matched}' not in clinic departments {clinic_depts}. Extracted: '{val}', Canonical: '{canonical}'")
                        s.draft[f] = None
                        dept_unavailable_ctx = build_booking_context(
                            s.draft, _missing_fields(s.draft),
                            department_unavailable=canonical,
                            clinic_departments=clinic_depts,
                            lang=lang,
                        )
                        # Do NOT return here — continue so remaining fields are processed
                    else:
                        logger.info(f"[Department update] Setting draft['department'] = '{matched}'")
                        s.draft[f] = matched
                else:
                    # Issue 5 fix: reject relationship words stored as patient name.
                    if f == "name" and (val in _RELATIONSHIP_WORDS or val.lower() in _RELATIONSHIP_WORDS):
                        logger.info(
                            f"[Draft update] Rejected relationship word as name: '{val}' — "
                            "system will ask for patient's actual name"
                        )
                    # Issue 10 fix: never overwrite a confirmed name or confirmed age.
                    # Once the caller confirms "yes, that's my name/age", that value is locked.
                    # Subsequent STT extractions may re-produce the uncorrected form (e.g.
                    # "Debjit Dash" after correction to "Debjit Das") — silently ignore them.
                    elif f == "name" and s.name_confirmed:
                        logger.info(
                            f"[Draft update] Skipping name '{val}' — name already confirmed "
                            f"as '{s.draft.get('name')}'"
                        )
                    elif f == "age" and s.age_confirmed:
                        logger.info(
                            f"[Draft update] Skipping age '{val}' — age already confirmed "
                            f"as '{s.draft.get('age')}'"
                        )
                    else:
                        s.draft[f] = val
                        logger.info(f"[Draft update] SET draft['{f}'] = '{val}'")

        logger.info(f"[Draft update] AFTER loop: draft={s.draft}")

        # ── Optional fields: accumulate doctor + age opportunistically ─────────
        # Doctor: store if mentioned; never ask for it
        _doctor_val = _normalize(extraction.get("doctor"))
        if _doctor_val and not s.draft.get("doctor"):
            s.draft["doctor"] = _doctor_val
            logger.info(f"[Draft update] Optional doctor stored: '{_doctor_val}'")
        # Age: store if mentioned; confirmation flow handles it downstream
        _age_val = _normalize(extraction.get("age"))
        if _age_val and not s.age_confirmed:
            # Reject if age already confirmed
            s.draft["age"] = _age_val
            logger.info(f"[Draft update] Optional age stored: '{_age_val}'")

        # If the extracted department wasn't available at this clinic, return now
        # (after the full loop so date/time from the same utterance were captured)
        if dept_unavailable_ctx is not None:
            return {"context": dept_unavailable_ctx, "end_call": False, "action": None}

        # ── Smart fallback: extract department from raw user_text if extraction missed it ──
        # Issue 7 fix: sarvam-30b sometimes returns null for short/phonetic department words
        # (e.g. "অর্থ" for Ortho, "dental" as a standalone word).
        # Issue 8 fix: ONLY use EXACT/case-insensitive direct dict lookup on the FULL user text.
        # NO partial matching (e.g. "ent" inside "appointment") and no multi-word sentences.
        # This prevents sentences like "I want to book for tomorrow" from spuriously
        # matching partial substrings like "ent" (in "appointment") → ENT.
        if not s.draft.get("department") and s.booking_started:
            _user_stripped = user_text.strip()
            _user_key = _user_stripped.lower()
            # Direct lookup only — not _normalize_department which does partial scanning
            _dept_direct = _DEPT_MAP.get(_user_key) or _DEPT_MAP.get(_user_stripped)
            if _dept_direct:
                _dept_matched = _match_to_clinic_departments(_dept_direct, clinic_depts)
                if not clinic_depts or _dept_matched in clinic_depts:
                    logger.info(
                        f"[Dept fallback] Direct match: '{_user_stripped}' → '{_dept_matched}' "
                        f"(extraction missed it)"
                    )
                    s.draft["department"] = _dept_matched

        # ── Name correction (if new name and not yet confirmed) ──
        if s.draft.get("name") and not s.name_confirmed and not s.awaiting_name_confirmation:
            raw_name = s.draft["name"]
            logger.info(f"[Name correction] Starting name correction for: '{raw_name}'")
            try:
                corrected, confidence, _ = await asyncio.wait_for(
                    correct_indian_name(raw_name), timeout=3.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"Name correction timed out for '{raw_name}', using as-is")
                corrected = raw_name
                confidence = "low"
            s.original_name = raw_name
            s.draft["name"] = corrected
            logger.info(f"[Name correction] corrected='{corrected}', confidence={confidence}")
            if confidence == "high":
                # High confidence — accept silently without asking the patient
                s.name_confirmed = True
                logger.info(f"[Name correction] High confidence — auto-accepted '{corrected}', skipping confirmation")
            else:
                # Low / medium confidence — ask the patient to confirm
                s.awaiting_name_confirmation = True
                logger.info(f"[Name correction] Set awaiting_name_confirmation=True (confidence={confidence})")
                ctx = build_booking_context(
                    s.draft, _missing_fields(s.draft),
                    name_to_confirm=corrected,
                    original_name=raw_name if raw_name != corrected else None,
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                logger.info(f"[Name correction] Returning name confirmation context (first 200 chars): {ctx[:200]}")
                return {"context": ctx, "end_call": False, "action": None}

        # ── Age confirmation (after age collected, before moving to department) ──
        if s.draft.get("age") and not s.age_confirmed and not s.awaiting_age_confirmation:
            age_val = s.draft["age"]
            logger.info(f"[Age confirmation] Setting awaiting_age_confirmation=True for age={age_val}")
            s.awaiting_age_confirmation = True
            ctx = build_booking_context(
                s.draft, _missing_fields(s.draft),
                age_to_confirm=age_val,
                clinic_departments=clinic_depts,
                lang=lang,
            )
            logger.info(f"[Age confirmation] Returning age confirmation context (first 200 chars): {ctx[:200]}")
            return {"context": ctx, "end_call": False, "action": None}

        # ──────────────────────────────────────────────────────────────────────
        # Confirmation RESPONSE handlers
        # These run AFTER the draft update loop so that any fields provided in
        # the same utterance as a confirmation answer are already in the draft.
        # ──────────────────────────────────────────────────────────────────────

        # ── Handle reschedule confirmation ──
        if s.awaiting_reschedule_confirm:
            confirm = extraction.get("confirm")
            if confirm is True:
                s.awaiting_reschedule_confirm = False
                s.intent = "reschedule"
            elif confirm is False:
                s.awaiting_reschedule_confirm = False
                s.existing_appointment = None
                s.intent = "book"
            else:
                ctx = build_booking_context(
                    s.draft, _missing_fields(s.draft),
                    existing_appointment=s.existing_appointment,
                    awaiting_reschedule_confirm=True,
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}

        # ── Handle name confirmation ──
        if s.awaiting_name_confirmation:
            logger.info(f"[Name confirmation] awaiting_name_confirmation=True, processing user response")
            confirm = extraction.get("confirm")
            has_other = any(extraction.get(f) for f in ["date", "time", "age", "department"])
            logger.info(f"[Name confirmation] confirm={confirm}, has_other={has_other}, extraction={extraction}")
            if has_other or confirm is True:
                logger.info(f"[Name confirmation] Accepting name implicitly (has_other={has_other}) or explicitly (confirm={confirm})")
                s.awaiting_name_confirmation = False
                s.name_confirmed = True
            elif confirm is False:
                logger.info("[Name confirmation] User rejected name, clearing and asking again")
                s.awaiting_name_confirmation = False
                s.name_confirmed = False
                s.draft["name"] = None
                s.original_name = None
                ctx = build_booking_context(s.draft, _missing_fields(s.draft), clinic_departments=clinic_depts, lang=lang)
                return {"context": ctx, "end_call": False, "action": None}
            else:
                new_name = _normalize(extraction.get("name"))
                if new_name:
                    logger.info(f"[Name confirmation] User gave new name: {new_name}, accepting")
                    s.draft["name"] = new_name
                    s.awaiting_name_confirmation = False
                    s.name_confirmed = True
                else:
                    logger.info(f"[Name confirmation] Still awaiting, returning name confirmation context")
                    ctx = build_booking_context(
                        s.draft, _missing_fields(s.draft),
                        name_to_confirm=s.draft.get("name"),
                        original_name=s.original_name,
                        clinic_departments=clinic_depts,
                        lang=lang,
                    )
                    return {"context": ctx, "end_call": False, "action": None}

        # ── Handle age confirmation ──
        if s.awaiting_age_confirmation:
            logger.info(f"[Age confirmation] awaiting_age_confirmation=True, processing user response")
            confirm = extraction.get("confirm")
            # NOTE: has_other intentionally NOT used here. Unlike name confirmation,
            # age must be confirmed explicitly — accepting it because the user also
            # mentioned a date/time bypasses the confirmation and records wrong ages.
            logger.info(f"[Age confirmation] confirm={confirm}, extraction={extraction}")
            if confirm is True:
                logger.info(f"[Age confirmation] Accepting age — explicit confirm=True")
                s.awaiting_age_confirmation = False
                s.age_confirmed = True
            elif confirm is False:
                # Issue 6 fix: only clear age on a PURE rejection.
                # If the user provided OTHER booking fields (department/date/time) in the
                # same utterance, extraction is hallucinating confirm=False for an unrelated
                # response (e.g. user said "Ortho" → extraction returned confirm=false).
                # In that case, keep the age and stay in age confirmation.
                _has_other_fields = any(extraction.get(f) for f in ["department", "date", "time"])
                if _has_other_fields:
                    logger.warning(
                        f"[Age confirmation] confirm=False but other fields also extracted "
                        f"({[f for f in ['department','date','time'] if extraction.get(f)]}) — "
                        "treating as unrelated response, keeping age and re-asking"
                    )
                    ctx = build_booking_context(
                        s.draft, _missing_fields(s.draft),
                        age_to_confirm=s.draft.get("age"),
                        clinic_departments=clinic_depts,
                        lang=lang,
                    )
                    return {"context": ctx, "end_call": False, "action": None}
                logger.info("[Age confirmation] User explicitly rejected age — clearing and asking again")
                s.awaiting_age_confirmation = False
                s.age_confirmed = False
                s.draft["age"] = None
                ctx = build_booking_context(s.draft, _missing_fields(s.draft), clinic_departments=clinic_depts, lang=lang)
                return {"context": ctx, "end_call": False, "action": None}
            else:
                new_age = _normalize(extraction.get("age"))
                if new_age:
                    logger.info(f"[Age confirmation] User gave new age: {new_age}, accepting")
                    s.draft["age"] = new_age
                    s.awaiting_age_confirmation = False
                    s.age_confirmed = True
                else:
                    logger.info(f"[Age confirmation] Still awaiting, returning age confirmation context")
                    ctx = build_booking_context(
                        s.draft, _missing_fields(s.draft),
                        age_to_confirm=s.draft.get("age"),
                        clinic_departments=clinic_depts,
                        lang=lang,
                    )
                    return {"context": ctx, "end_call": False, "action": None}

        # ── Handle time clarification (AM/PM) ──
        if s.awaiting_time_clarification and s.time_clarification_value:
            text_lower = user_text.lower()
            if any(w in text_lower for w in ["morning", "am", "subah", "সকাল", "காலை"]):
                s.draft["time"] = s.time_clarification_value.replace("PM", "AM")
                s.awaiting_time_clarification = False
                s.time_clarification_value = None
            elif any(w in text_lower for w in ["evening", "pm", "shaam", "সন্ধ্যা", "மாலை", "afternoon"]):
                s.draft["time"] = s.time_clarification_value.replace("AM", "PM")
                s.awaiting_time_clarification = False
                s.time_clarification_value = None
            else:
                ctx = build_booking_context(
                    s.draft, _missing_fields(s.draft),
                    time_clarification=s.time_clarification_value,
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}

        # ── Handle booking confirmation ──
        if s.awaiting_confirmation:
            # ── Handle time change DURING confirmation ──────────────────────────────────
            # If the user selects a DIFFERENT time while we're waiting for yes/no,
            # treat it as a time correction (not a confirmation).  Clear the
            # confirmation state and let the slot-check gate re-run below.
            # This fixes the "Six" → hallucinated booking bug (Turn 15, 18:03 log).
            new_time_raw = extraction.get("time")
            if new_time_raw:
                parsed_new_time = _parse_time_text(new_time_raw) if not _is_valid_time_format(new_time_raw) else new_time_raw
                parsed_new_time = parsed_new_time or new_time_raw
                if _is_valid_time_format(parsed_new_time) and parsed_new_time != s.draft.get("time"):
                    logger.info(
                        f"[Confirmation time change] User gave new time '{parsed_new_time}' "
                        f"(was '{s.draft.get('time')}') — re-opening confirmation"
                    )
                    s.awaiting_confirmation = False
                    s.confirmation_presented_to_llm = False
                    s.draft["time"] = parsed_new_time
                    # Fall through to slot-check gate below (s.awaiting_confirmation is now False)

            # Only proceed with confirmation logic if still in confirmation state
            # (time-change handler above may have cleared it)
            if s.awaiting_confirmation:
                # ── Guard: ensure LLM has had at least one turn to present the confirmation question ──
                # The turn that SETS awaiting_confirmation returns immediately (line ~992 path).
                # On the NEXT turn, if confirm=True comes in as noise/filler before the LLM has
                # asked "May I confirm?", this guard fires once to give the LLM that chance.
                if not s.confirmation_presented_to_llm:
                    s.confirmation_presented_to_llm = True
                    logger.info("[Confirmation guard] First handler pass — letting LLM present confirmation question")
                    ctx = build_booking_context(
                        s.draft, [],
                        awaiting_confirmation=True,
                        clinic_departments=clinic_depts,
                        lang=lang,
                    )
                    return {"context": ctx, "end_call": False, "action": None}

                confirm = extraction.get("confirm")
                if confirm is True:
                    missing_now = _missing_fields(s.draft)
                    if missing_now:
                        logger.warning(f"[Pre-booking validation] Missing fields at confirmation: {missing_now}")
                        s.awaiting_confirmation = False
                        ctx = build_booking_context(
                            s.draft, missing_now,
                            validation_failed=True,
                            clinic_departments=clinic_depts,
                            lang=lang,
                        )
                        return {"context": ctx, "end_call": False, "action": None}
                    result = await self._reserve(s)
                    if result.get("success"):
                        s.booking_complete = True
                        s.awaiting_contact_confirmation = True
                        s.contact_number = s.phone  # pre-fill with caller's number
                        logger.info("[_reserve] Success — set booking_complete=True, starting contact confirmation")
                    ctx = build_booking_context(s.draft, [], booking_result=result, lang=lang)
                    end_call = False  # stay on call for contact confirmation
                    return {"context": ctx, "end_call": end_call, "action": None}
                elif confirm is False:
                    s.awaiting_confirmation = False
                    ctx = build_booking_context(
                        s.draft, _missing_fields(s.draft),
                        user_wants_change=True,
                        clinic_departments=clinic_depts,
                        lang=lang,
                    )
                    return {"context": ctx, "end_call": False, "action": None}
                # Ambiguous — user may have provided new info; fall through to collect remaining fields

        # ─────────────────────────────────────────────────────────────────────

        # ── Time inference (AM/PM clarification) ──
        if s.draft.get("time") and s.draft.get("date") and not s.awaiting_time_clarification:
            resolved, needs_clarify = _smart_time_inference(
                s.draft["time"], s.draft["date"], now
            )
            if needs_clarify:
                s.awaiting_time_clarification = True
                s.time_clarification_value = s.draft["time"]
                ctx = build_booking_context(
                    s.draft, _missing_fields(s.draft),
                    time_clarification=s.draft["time"],
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}
            elif resolved:
                s.draft["time"] = resolved

        # ── Map department from problem description via LLM ──
        if s.draft.get("department") and not s.draft["department"].replace(" ", "").isalpha():
            # If it looks like a problem description, try to resolve later
            pass

        # ── Check if all fields collected → confirm ──
        missing = _missing_fields(s.draft)
        if not missing and not s.awaiting_confirmation:
            # ✅ PHASE 3: Double-check validation before setting awaiting_confirmation
            # This is defensive - ensure ALL required fields are truly present
            final_check = _missing_fields(s.draft)
            if final_check:
                logger.warning(f"[Pre-confirmation validation] Fields still missing: {final_check}")
                # Continue with normal field collection flow (don't set awaiting_confirmation)
                ctx = build_booking_context(
                    s.draft, [final_check[0]],  # Ask for first missing field
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}

            # Check slot availability before asking confirmation
            slot_check = await self._check_slot(s)
            if slot_check.get("available"):
                # If the user already confirmed in this same utterance (e.g. they
                # corrected a field during confirmation → confirm=False cleared
                # awaiting_confirmation → then said "yes" on the very next turn),
                # book immediately instead of looping through confirmation again.
                # IMPORTANT: only skip re-confirmation when the user gave NO new
                # field values (pure "yes/yeah/please").  If they also provided a
                # field (e.g. "Go ahead with 5:30 PM"), they are selecting a slot
                # while implicitly confirming — we still need to show the summary.
                is_pure_confirm = extraction.get("confirm") is True and not any(
                    extraction.get(f) for f in FIELD_ORDER
                )
                if is_pure_confirm:
                    # Hard safety check — must re-validate missing fields even on the
                    # shortcut path (age_confirmed guard above should have caught it,
                    # but be defensive).
                    _pure_confirm_missing = _missing_fields(s.draft)
                    if _pure_confirm_missing:
                        logger.error(
                            f"[Slot-check gate] is_pure_confirm but missing fields: "
                            f"{_pure_confirm_missing} — blocking booking"
                        )
                        s.awaiting_confirmation = False
                        ctx = build_booking_context(
                            s.draft, _pure_confirm_missing,
                            validation_failed=True,
                            clinic_departments=clinic_depts,
                            lang=lang,
                        )
                        return {"context": ctx, "end_call": False, "action": None}
                    logger.info("[Slot-check gate] pure confirm=True (no new fields) — booking immediately without re-confirming")
                    s.awaiting_confirmation = True  # set so _reserve / context look right
                    result = await self._reserve(s)
                    if result.get("success"):
                        s.booking_complete = True
                        s.awaiting_contact_confirmation = True
                        s.contact_number = s.phone  # pre-fill with caller's number
                        logger.info("[_reserve] Success — set booking_complete=True, starting contact confirmation")
                    ctx = build_booking_context(s.draft, [], booking_result=result, lang=lang)
                    end_call = False  # stay on call for contact confirmation
                    return {"context": ctx, "end_call": end_call, "action": None}

                s.awaiting_confirmation = True
                s.confirmation_presented_to_llm = False  # reset so guard fires on next turn
                ctx = build_booking_context(
                    s.draft, [],
                    awaiting_confirmation=True,
                    clinic_departments=clinic_depts,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}
            else:
                # Slot not available — inject alternatives
                ctx = build_booking_context(s.draft, [], booking_result=slot_check, clinic_departments=clinic_depts, lang=lang)
                s.draft["time"] = None  # Clear time so user can pick new one
                # ✅ Return booking_result so session.py can inject immediate follow-up
                return {"context": ctx, "end_call": False, "action": None, "booking_result": slot_check}

        # ── Fetch slot availability if date is known (for context) ──
        # Only show available slots once name, age, and department are collected.
        # Showing slots earlier causes the LLM to skip collecting core fields.
        # Use a short timeout so slot fetch never blocks the LLM response.
        slot_info = None
        core_complete = all(s.draft.get(f) for f in ["name", "age", "department"])
        if core_complete and s.draft.get("date") and "time" in missing:
            try:
                slot_info = await asyncio.wait_for(self._fetch_slots(s), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Slot fetch timed out — continuing without slot info")

        # When slots are available, pass them as slot_options so context.py renders
        # the [Available Slots] block and the LLM presents the choices to the user.
        _slot_options = (slot_info.get("available_times") or []) if slot_info else None
        
        # ✅ FIX: Limit to 3 slots and filter past times for better UX
        if _slot_options:
            # Filter out past times if booking for today
            try:
                date_str = s.draft.get("date", "")
                today = datetime.now().strftime("%-d %b") if hasattr(datetime.now(), "strftime") else datetime.now().strftime("%#d %b" if os.name == "nt" else "%-d %b")
                if date_str == today:
                    current_hour = datetime.now().hour
                    current_minute = datetime.now().minute
                    filtered_slots = []
                    for t in _slot_options:
                        # Parse hour from time string like "03:00 PM"
                        try:
                            time_parts = t.split()
                            if len(time_parts) >= 2:
                                hour_min = time_parts[0].split(":")
                                hour = int(hour_min[0])
                                minute = int(hour_min[1]) if len(hour_min) > 1 else 0
                                period = time_parts[1].upper()
                                # Convert to 24-hour format
                                if period == "PM" and hour != 12:
                                    hour += 12
                                elif period == "AM" and hour == 12:
                                    hour = 0
                                # Only keep future times
                                if hour > current_hour or (hour == current_hour and minute > current_minute):
                                    filtered_slots.append(t)
                        except:
                            filtered_slots.append(t)  # Keep if can't parse
                    _slot_options = filtered_slots if filtered_slots else _slot_options
            except:
                pass  # Keep all slots if filtering fails
            
            # Limit to first 3 slots for better voice UX
            _slot_options = _slot_options[:3]
        
        # Track offered slots for confirmation handling (initialize if missing)
        if not hasattr(s, "last_offered_times"):
            s.last_offered_times = []
        if _slot_options:
            s.last_offered_times = _slot_options
            logger.info(f"[Slot tracking] Stored {len(_slot_options)} offered times: {_slot_options}")
        
        # ── Hard field order enforcement ──
        # ONLY pass the FIRST missing field to context builder. This forces Python-level
        # validation that the LLM can't skip fields even if it tries.
        fields_to_collect = [missing[0]] if missing else []
        
        ctx = build_booking_context(
            s.draft, fields_to_collect,
            slot_info=slot_info,
            slot_options=_slot_options or None,
            clinic_departments=clinic_depts,
            lang=lang,
        )
        logger.info(f"[process_turn EXIT] session_id={session_id}, manager_id={id(self)}, session_obj_id={id(s)}")
        logger.info(f"[process_turn EXIT] Returning context for field: {fields_to_collect}")
        logger.info(f"[process_turn EXIT] Draft: {s.draft}, Missing (all): {missing}, name_confirmed={s.name_confirmed}, age_confirmed={s.age_confirmed}")
        logger.info(f"[process_turn EXIT] self.sessions has {len(self.sessions)} session(s): {list(self.sessions.keys())}")
        return {"context": ctx, "end_call": False, "action": None}

    # ──────────────────────────────────────────────────────────
    #  Post-booking contact confirmation handlers
    # ──────────────────────────────────────────────────────────
    async def _handle_contact_confirmation(self, s: "BookingSession", extraction: dict, lang: str = "en") -> dict:
        """Handle post-booking contact number confirmation turn."""
        contact_num = _normalize(extraction.get("contact_number"))
        confirm = extraction.get("confirm")
        user_text_lower = (extraction.get("_user_text", "") or "").lower()

        # "same"/"this number"/"yes" → use caller's own number
        if confirm is True or any(w in user_text_lower for w in ["same", "this number", "this", "haan", "yes"]):
            s.contact_number = s.phone
            s.awaiting_contact_confirmation = False
            s.awaiting_channel_selection = True
            logger.info(f"[Contact confirmation] Using caller's own number: {s.phone}")
        elif contact_num:
            s.contact_number = contact_num
            s.awaiting_contact_confirmation = False
            s.awaiting_channel_selection = True
            logger.info(f"[Contact confirmation] Using alternate number: {contact_num}")
        else:
            # Still waiting — return context to ask again
            ctx = build_booking_context(
                s.draft, [],
                awaiting_contact_confirmation=True,
                caller_phone=s.phone,
                lang=lang,
            )
            return {"context": ctx, "end_call": False, "action": None}

        ctx = build_booking_context(
            s.draft, [],
            awaiting_channel_selection=True,
            contact_number=s.contact_number,
            lang=lang,
        )
        return {"context": ctx, "end_call": False, "action": None}

    async def _handle_channel_selection(self, s: "BookingSession", extraction: dict, lang: str = "en") -> dict:
        """Handle post-booking communication channel selection turn."""
        channel = _normalize(extraction.get("preferred_channel"))
        email = _normalize(extraction.get("email_address"))
        user_text_lower = (extraction.get("_user_text", "") or "").lower()

        # Detect channel from user text if extraction missed it
        if not channel:
            if any(w in user_text_lower for w in ["whatsapp", "what's app", "wa"]):
                channel = "whatsapp"
            elif any(w in user_text_lower for w in ["sms", "text", "message", "msg"]):
                channel = "sms"
            elif any(w in user_text_lower for w in ["email", "mail", "e-mail"]):
                channel = "email"
            elif any(w in user_text_lower for w in ["call", "voice", "phone", "ring"]):
                channel = "voice"

        if channel:
            s.preferred_channel = channel
            if channel == "email" and not email and not s.email_address:
                # Need email address — stay in channel selection sub-state
                ctx = build_booking_context(
                    s.draft, [],
                    awaiting_email=True,
                    contact_number=s.contact_number,
                    lang=lang,
                )
                return {"context": ctx, "end_call": False, "action": None}
            if email:
                s.email_address = email
            s.awaiting_channel_selection = False
            logger.info(f"[Channel selection] Channel={channel}, contact={s.contact_number}, email={s.email_address}")
            ctx = build_booking_context(
                s.draft, [],
                contact_confirmed=True,
                contact_number=s.contact_number,
                preferred_channel=channel,
                email_address=s.email_address,
                lang=lang,
            )
            return {"context": ctx, "end_call": True, "action": None}

        # Still waiting — return context to ask again
        ctx = build_booking_context(
            s.draft, [],
            awaiting_channel_selection=True,
            contact_number=s.contact_number,
            lang=lang,
        )
        return {"context": ctx, "end_call": False, "action": None}

    # ──────────────────────────────────────────────────────────
    #  Human transfer detection
    # ──────────────────────────────────────────────────────────
    def _detect_transfer_need(self, user_text: str, s: "BookingSession") -> bool:
        """Return True if the call should be transferred to a human agent."""
        text_lower = user_text.lower()
        # Medical emergency keywords
        emergency_keywords = [
            "emergency", "chest pain", "heart attack", "ambulance", "critical",
            "unconscious", "bleeding", "can't breathe", "cannot breathe", "stroke",
            # Hindi
            "आपातकाल", "इमर्जेंसी", "एम्बुलेंस",
            # Bengali
            "জরুরি", "অ্যাম্বুলেন্স",
        ]
        if any(kw in text_lower for kw in emergency_keywords):
            logger.info(f"[Transfer] Emergency keyword detected: '{user_text[:60]}'")
            return True
        # Frustration / explicit human request
        frustration_keywords = [
            "real person", "human", "agent", "manager", "supervisor", "speak to someone",
            "talk to someone", "useless", "not working", "waste of time", "transfer me",
            "connect me", "helpless", "frustrated", "annoying",
            # Hindi
            "इंसान से बात", "असली इंसान", "मैनेजर",
            # Bengali
            "মানুষের সাথে কথা", "ম্যানেজার",
        ]
        if any(kw in text_lower for kw in frustration_keywords):
            logger.info(f"[Transfer] Frustration/human-request keyword detected: '{user_text[:60]}'")
            return True
        # Repeated booking failures
        if s.reserve_fail_count >= 2:
            logger.info(f"[Transfer] reserve_fail_count={s.reserve_fail_count} >= 2 — triggering transfer")
            return True
        return False

    # ──────────────────────────────────────────────────────────
    #  Backend API helpers
    # ──────────────────────────────────────────────────────────
    async def _get_clinic_departments(self, clinic_id: str) -> list[str]:
        """Fetch and cache the clinic's department list from the backend."""
        if clinic_id in self._clinic_depts_cache:
            return self._clinic_depts_cache[clinic_id]
        try:
            clinic = await self.backend.get_clinic(clinic_id)
            depts = clinic.get("departments", []) if clinic else []
            self._clinic_depts_cache[clinic_id] = depts
            logger.info(f"Clinic {clinic_id} departments: {depts}")
            return depts
        except Exception as e:
            logger.warning(f"Could not fetch clinic departments for {clinic_id}: {e}")
            self._clinic_depts_cache[clinic_id] = []  # cache empty to avoid repeated failures
            return []

    async def _check_existing(self, clinic_id: str, phone: str, now: datetime) -> Optional[dict]:
        try:
            appointments = await self.backend.list_appointments(clinic_id=clinic_id, phone=phone)
            if not appointments:
                return None
            tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
            for apt in appointments:
                apt_at = apt.get("appointment_at")
                if apt_at:
                    try:
                        apt_dt = datetime.fromisoformat(str(apt_at).replace("Z", "+00:00"))
                        apt_dt = apt_dt.astimezone(ZoneInfo(tz))
                        if apt_dt > now:
                            return apt
                    except Exception:
                        continue
            return None
        except Exception as e:
            logger.error(f"Check existing appointments error: {e}")
            return None

    async def _fetch_slots(self, s: BookingSession) -> Optional[dict]:
        try:
            result = await self.backend.available_slots(s.clinic_id, s.draft["date"])
            times = result.get("available_times", [])
            if not times:
                return None
            # Filter by vague period preference if user said "morning/afternoon/evening/night"
            vague = (s.draft.get("time") or "").lower()
            if vague and not _is_valid_time_format(vague) and vague in _PERIOD_HOUR_RANGES:
                times = _filter_slots_by_period(times, vague)
            return {"available_times": times, "preferred_period": vague or None}
        except Exception as e:
            logger.error(f"Fetch slots error: {e}")
            return None

    async def _check_slot(self, s: BookingSession) -> dict:
        """Check if the requested slot is available. Returns result dict."""
        try:
            avail = await self.backend.slot_availability(
                s.clinic_id, s.draft["date"], s.draft["time"]
            )
            if avail.get("available", 0) > 0:
                return {"available": True}

            # Slot full — find alternatives
            all_slots = await self.backend.available_slots(s.clinic_id, s.draft["date"])
            times = all_slots.get("available_times", [])
            nearest = _find_nearest_slot(s.draft["time"], times) if times else None
            return {
                "slot_full": True,
                "available_times": times,
                "nearest_time": nearest,
            }
        except Exception as e:
            logger.error(f"Slot check error: {e}")
            return {"error": str(e)}

    async def _reserve(self, s: BookingSession) -> dict:
        """Reserve the appointment via Backend_saas."""
        # CRITICAL: Final hard gate — refuse to book if ANY required field is missing.
        logger.info(f"[_reserve] FINAL VALIDATION - Draft before booking: {s.draft}")
        missing = _missing_fields(s.draft)
        if missing:
            logger.error(f"[_reserve] BLOCKED — missing fields: {missing}")
            s.reserve_fail_count += 1
            return {"error": f"Cannot book - missing required fields: {missing}"}
        
        try:
            # Ensure user exists
            user = await self.backend.get_or_create_user(s.phone, name=s.draft.get("name"))
            s.user_id = user.get("id") or user.get("_id")

            # Ensure user-clinic link
            if s.user_id:
                await self.backend.get_or_create_user_clinic(str(s.user_id), s.clinic_id)

            # ── Build appointment_at as ISO datetime ──
            appointment_at: Optional[str] = None
            try:
                tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
                date_str = s.draft.get("date", "")
                time_str = s.draft.get("time", "")
                now_tz = datetime.now(ZoneInfo(tz))
                current_year = now_tz.year
                apt_dt = datetime.strptime(
                    f"{date_str} {current_year} {time_str}", "%d %b %Y %I:%M %p"
                ).replace(tzinfo=ZoneInfo(tz))
                # If the parsed date is in the past, assume next year
                if apt_dt < now_tz:
                    apt_dt = apt_dt.replace(year=current_year + 1)
                appointment_at = apt_dt.isoformat()
            except Exception:
                appointment_at = None

            payload = {
                "phone": s.phone,
                "clinic_id": s.clinic_id,
                "name": s.draft.get("name", ""),
                "date": s.draft.get("date", ""),
                "time": s.draft.get("time", ""),
            }
            if s.user_id:
                payload["user_id"] = str(s.user_id)
            if s.draft.get("age"):
                payload["age"] = s.draft["age"]
            if s.draft.get("department"):
                # Department in draft is already clinic-matched from process_turn;
                # re-normalize only as a safety net (no extra API call needed).
                payload["department"] = s.draft["department"]
            if s.draft.get("doctor"):
                payload["doctor"] = s.draft["doctor"]
            if s.draft.get("reason"):
                payload["reason"] = s.draft["reason"]
            if appointment_at:
                payload["appointment_at"] = appointment_at

            logger.info(f"[_reserve] Payload: {payload}")
            response = await self.backend.reserve_appointment(payload)
            logger.info(f"[_reserve] Response: {response.status_code} {response.text[:300]}")

            if response.status_code == 201:
                logger.info(f"Appointment reserved: {response.json()}")
                return {"success": True, "appointment": response.json()}
            elif response.status_code == 409:
                logger.warning("Slot full at reservation time")
                s.reserve_fail_count += 1
                try:
                    all_slots = await self.backend.available_slots(s.clinic_id, s.draft["date"])
                    times = all_slots.get("available_times", [])
                except Exception as _sa:
                    logger.warning(f"available_slots fetch failed: {_sa}")
                    times = []
                nearest = _find_nearest_slot(s.draft["time"], times) if times else None
                return {"slot_full": True, "available_times": times, "nearest_time": nearest}
            else:
                logger.error(f"Reserve failed: {response.status_code} {response.text}")
                s.reserve_fail_count += 1
                return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}

        except Exception as e:
            logger.error(f"Reservation error: {e}")
            s.reserve_fail_count += 1
            return {"error": str(e)}
