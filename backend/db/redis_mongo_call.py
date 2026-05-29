# redis_mongo_call.py
# 📞 Redis helpers for AI Voice Call sessions (CallSid-based)

import os
import redis
from dotenv import load_dotenv
from datetime import datetime, timezone

# --------------------------------------------------
# Load ENV (same pattern as redis_mongo.py)
# --------------------------------------------------
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# --------------------------------------------------
# Redis Setup (reuse same Redis instance)
# --------------------------------------------------
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True
)

# --------------------------------------------------
# TTL CONFIG (call-safe)
# --------------------------------------------------
CALL_SESSION_TTL_SECONDS = int(os.getenv("CALL_SESSION_TTL_SECONDS", 600))  # default 10 mins


# --------------------------------------------------
# 📞 CALL SESSION MANAGEMENT
# Key format:
#   call:{clinic_id}:{call_sid}
# --------------------------------------------------

def create_call_session(call_sid, clinic_id, from_phone=None, to_phone=None):
    """
    Create a new call session in Redis.
    This is independent of WhatsApp / QR sessions.
    """
    key = f"call:{clinic_id}:{call_sid}"

    data = {
        "call_sid": call_sid,
        "clinic_id": clinic_id,
        "from_phone": from_phone,
        "to_phone": to_phone,
        "language": "en",
        "step": "GREETING",
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    try:
        redis_client.hset(key, mapping=data)
        redis_client.expire(key, CALL_SESSION_TTL_SECONDS)

        ttl_now = redis_client.ttl(key)
        print(f"[CALL_SESSION_CREATED] key={key} ttl={ttl_now}")

        return data

    except Exception as e:
        print(f"[ERROR] create_call_session failed for {key}: {e}")
        return None


def get_call_session(call_sid, clinic_id):
    """
    Fetch call session from Redis.
    Returns dict or None.
    """
    key = f"call:{clinic_id}:{call_sid}"
    try:
        data = redis_client.hgetall(key)
        ttl = redis_client.ttl(key)

        print(f"[GET_CALL_SESSION] key={key} ttl={ttl} data={data}")

        if data:
            return data
        return None

    except Exception as e:
        print(f"[ERROR] get_call_session failed for {key}: {e}")
        return None


def update_call_session(call_sid, clinic_id, updates: dict):
    """
    Update fields in call session and refresh TTL.
    """
    key = f"call:{clinic_id}:{call_sid}"
    try:
        redis_client.hset(key, mapping=updates)
        redis_client.expire(key, CALL_SESSION_TTL_SECONDS)

        ttl_now = redis_client.ttl(key)
        print(f"[UPDATE_CALL_SESSION] key={key} ttl={ttl_now} updates={updates}")

    except Exception as e:
        print(f"[ERROR] update_call_session failed for {key}: {e}")


def delete_call_session(call_sid, clinic_id):
    """
    Explicitly delete call session (optional cleanup).
    """
    key = f"call:{clinic_id}:{call_sid}"
    try:
        redis_client.delete(key)
        print(f"[CALL_SESSION_DELETED] key={key}")
    except Exception as e:
        print(f"[ERROR] delete_call_session failed for {key}: {e}")
