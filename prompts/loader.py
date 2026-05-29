"""
Prompt loader — returns language-specific system prompt with optional clinic context.

Usage:
    from prompts.loader import get_system_prompt
    prompt = get_system_prompt("hi")
    prompt = get_system_prompt("en", clinic_name="HealthFirst Clinic", departments=["Dental", "General"])
"""
import logging
import importlib

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
