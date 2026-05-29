# session_store.py
import time
from threading import Lock

_sessions = {}
_lock = Lock()

def create_session(phone, ttl_seconds):
    with _lock:
        _sessions[phone] = {
            "data": {
                "current_flow": "",
                "step": "start",
                "is_onboarding": True,
                "last_step_time": time.time()
            },
            "expires_at": time.time() + ttl_seconds
        }

def get_session(phone):
    with _lock:
        s = _sessions.get(phone)
        if not s:
            return None

        if time.time() > s["expires_at"]:
            # session expired → delete it
            del _sessions[phone]
            return None

        return s["data"]

def update_session(phone, data, ttl_seconds):
    with _lock:
        if phone not in _sessions:
            return

        # update session fields
        _sessions[phone]["data"].update(data)

        # reset TTL
        _sessions[phone]["expires_at"] = time.time() + ttl_seconds
