"""
Prompt loader — returns the system prompt for the voice agent.

Two modes:
  get_smart_prompt()   — NEW: single universal prompt (recommended).
                          Works for all languages; sarvam-30b adapts naturally.
  get_system_prompt()  — LEGACY: per-language prompt files (en.py, hi.py, …).
                          Kept for reference / rollback.

Usage:
    from prompts.loader import get_smart_prompt
    prompt = get_smart_prompt(clinic_name="HealthFirst", departments=["Dental"], caller_phone="+919999999999")
"""
import os
import logging
import importlib
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("prompts")

_CACHE: dict[str, str] = {}

_SUPPORTED = {"en", "hi", "bn", "ta", "te", "kn", "ml", "mr", "gu", "pa", "od"}


def get_system_prompt(
    lang_code: str = "en",
    clinic_name: str | None = None,
    departments: list[str] | None = None,
    working_hours: str | None = None,
) -> str:
    """Return the system prompt for the given language.

    If clinic_name / departments / working_hours are provided,
    they replace the placeholders in the base prompt.
    """
    if lang_code not in _SUPPORTED:
        lang_code = "en"

    if lang_code not in _CACHE:
        try:
            # Import the language-specific prompt module
            module = importlib.import_module(f"prompts.{lang_code}")
            _CACHE[lang_code] = module.SYSTEM_PROMPT
            logger.debug(f"Loaded prompt for '{lang_code}' from prompts.{lang_code}")
        except Exception as e:
            logger.error(f"Failed loading prompt for '{lang_code}': {e}")
            _CACHE[lang_code] = "You are a helpful clinic receptionist. Help the patient book an appointment."

    prompt = _CACHE[lang_code]

    # Inject clinic-specific info
    prompt = prompt.replace("{clinic_name}", clinic_name or "AI Smart Clinic")
    prompt = prompt.replace("{departments}", ", ".join(departments) if departments else "General, Dental, Dermatology, Orthopedic, Pediatric")
    prompt = prompt.replace("{working_hours}", working_hours or "9 AM to 6 PM, Monday to Saturday")

    return prompt


# ── NEW: Universal smart prompt ───────────────────────────────────────────────

_SMART_PROMPT_TEMPLATE: str | None = None


def get_smart_prompt(
    clinic_name: str | None = None,
    departments: list[str] | None = None,
    working_hours: str | None = None,
    caller_phone: str | None = None,
) -> str:
    """
    Return the universal smart system prompt (language-agnostic).

    All placeholders are filled with the provided values or sensible defaults.
    The prompt instructs sarvam-30b to start in English and switch language
    only when the caller explicitly requests it — no per-language prompt needed.
    """
    global _SMART_PROMPT_TEMPLATE
    if _SMART_PROMPT_TEMPLATE is None:
        from prompts.base import SYSTEM_PROMPT
        _SMART_PROMPT_TEMPLATE = SYSTEM_PROMPT

    # Compute today's date in the clinic's timezone
    tz_name = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    today_date = datetime.now(tz).strftime("%A, %d %B %Y")  # e.g. "Wednesday, 18 June 2026"

    prompt = _SMART_PROMPT_TEMPLATE
    prompt = prompt.replace("{clinic_name}", clinic_name or "our clinic")
    prompt = prompt.replace("{departments}", ", ".join(departments) if departments else "General, Dental, Dermatology, Orthopedic, Pediatric")
    prompt = prompt.replace("{working_hours}", working_hours or "9 AM to 6 PM, Monday to Saturday")
    prompt = prompt.replace("{caller_phone}", caller_phone or "not available")
    prompt = prompt.replace("{today_date}", today_date)

    return prompt
