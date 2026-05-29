# queue_service/token_service.py
import random
from datetime import datetime
from db.redis_mongo import redis_client, clinics_col

def _today():
    return datetime.now().strftime("%Y-%m-%d")

def _token_key(clinic_id):
    return f"token:counter:{clinic_id}:{_today()}"

def _base_key(clinic_id):
    return f"token:base:{clinic_id}:{_today()}"

def _clinic_prefix(clinic):
    # Use clinic name initials, fallback to CL
    name = clinic.get("name", "")
    parts = [p[0].upper() for p in name.split() if p]
    return "".join(parts[:2]) or "CL"

def generate_token(clinic_id):
    """
    Returns a semi-random sequential token like SB-143
    """
    clinic = clinics_col.find_one({"_id": clinic_id})
    if not clinic:
        raise ValueError("Clinic not found")

    prefix = _clinic_prefix(clinic)

    # 1️⃣ Ensure daily base exists
    base_key = _base_key(clinic_id)
    counter_key = _token_key(clinic_id)

    if not redis_client.exists(base_key):
        base = random.randint(100, 999)
        redis_client.set(base_key, base)
        redis_client.set(counter_key, base)
    else:
        base = int(redis_client.get(base_key))

    # 2️⃣ Increment counter
    current = redis_client.incr(counter_key)

    return f"{prefix}-{current}"

# def generate_token(clinic_id):
#     clinic = clinics_col.find_one({"_id": clinic_id})
#     if not clinic:
#         raise ValueError("Clinic not found")

#     prefix = _clinic_prefix(clinic)
#     key = _token_key(clinic_id)

#     # 🔒 Atomic, deterministic, safe
#     seq = redis_client.incr(key)

#     # Optional: expire after 2 days
#     redis_client.expire(key, 48 * 60 * 60)

#     return f"{prefix}-{seq:03d}"
