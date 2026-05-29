import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")


def get_clinic_timezone(clinic) -> str:
    if not clinic:
        return DEFAULT_TIMEZONE
    slot_settings = clinic.get("slot_settings") or {}
    return slot_settings.get("timezone") or DEFAULT_TIMEZONE


def now_in_clinic(clinic):
    tz_name = get_clinic_timezone(clinic)
    if ZoneInfo is None:
        return datetime.now()
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(ZoneInfo(DEFAULT_TIMEZONE))