# # queue_engine.py
# import json
# import time
# from datetime import datetime, timedelta
# import uuid
# from bson import ObjectId
# from db.redis_mongo import redis_client, appointments_col, clinics_col

# # ---------------- CONFIG ----------------
# WAIT_WEIGHT = 0.02
# EARLY_FILL_MIN = 15
# LATE_GRACE_MIN = 7

# # ---------------- HELPERS ----------------
# def today_key(date=None):
#     return (date or datetime.now()).strftime("%Y-%m-%d")


# def active_count(clinic_id):
#     return len(redis_client.keys(
#         f"active:{clinic_id}:{today_key()}:*"
#     ))


# def mark_active(clinic_id, payload):
#     key = f"active:{clinic_id}:{today_key()}:{payload['appointment_id']}"
#     redis_client.hset(key, mapping={
#         "appointment_id": payload["appointment_id"],
#         "name": payload["name"],
#         "phone": payload["phone"],
#         "active_since": datetime.now().isoformat()
#     })
#     redis_client.expire(key, 2 * 60 * 60)


# # ---------------- PRIORITY ----------------
# def calculate_queue_score(base, checkin_time):
#     wait_minutes = max(
#         (datetime.now() - checkin_time).total_seconds() / 60,
#         0
#     )
#     return round(base - (wait_minutes * WAIT_WEIGHT), 3)


# # ---------------- BUFFER DECISION ----------------
# def buffer_type(appointment, checkin_time):
#     appt_time = appointment["appointment_at"]
#     late_cutoff = appt_time + timedelta(minutes=LATE_GRACE_MIN)

#     if appointment.get("is_walkin"):
#         return "late"

#     if checkin_time > late_cutoff:
#         return "late"

#     diff_min = (appt_time - checkin_time).total_seconds() / 60

#     # Early but within EARLY_FILL window → eligible NOW
#     if 0 <= diff_min <= EARLY_FILL_MIN:
#         return None  # goes directly to QUEUE

#     # Early but too soon → UPCOMING
#     if diff_min > EARLY_FILL_MIN:
#         return "early"

#     # On-time or late within grace → QUEUE
#     return None


# # ---------------- ADD TO BUFFER ----------------
# def add_to_buffer(appointment, clinic_id, checkin_time, btype):
#     key = f"buffer:{btype}:{clinic_id}:{today_key()}"

#     payload = {
#         "appointment_id": str(appointment["_id"]),
#         "name": appointment["name"],
#         "phone": appointment["phone"],
#         "slot_at": appointment["appointment_at"].isoformat(),
#         "checkin_at": checkin_time.isoformat(),
#         "buffer_type": btype
#     }

#     redis_client.zadd(
#         key,
#         {json.dumps(payload): checkin_time.timestamp()}
#     )

#     return {"status": f"buffered_{btype}"}

# def remove_from_all_states(clinic_id, appointment_id):
#     today = today_key()
#     aid = str(appointment_id)

#     # ---------- QUEUE ----------
#     queue_key = f"queue:{clinic_id}:{today}"
#     for item in redis_client.zrange(queue_key, 0, -1):
#         if json.loads(item).get("appointment_id") == aid:
#             redis_client.zrem(queue_key, item)

#     # ---------- BUFFER EARLY ----------
#     early_key = f"buffer:early:{clinic_id}:{today}"
#     for item in redis_client.zrange(early_key, 0, -1):
#         if json.loads(item).get("appointment_id") == aid:
#             redis_client.zrem(early_key, item)

#     # ---------- BUFFER LATE ----------
#     late_key = f"buffer:late:{clinic_id}:{today}"
#     for item in redis_client.zrange(late_key, 0, -1):
#         if json.loads(item).get("appointment_id") == aid:
#             redis_client.zrem(late_key, item)




# # ---------------- ADD TO QUEUE ----------------
# def add_to_queue(appointment_id, clinic_id, checkin_time=None):
#     checkin_time = checkin_time or datetime.now()

#     appointment = appointments_col.find_one({"_id": ObjectId(appointment_id)})
#     clinic = clinics_col.find_one({"_id": clinic_id})
#     if not appointment or not clinic:
#         raise ValueError("Invalid appointment or clinic")
    
#     # 🔥 ADD THIS LINE HERE (VERY IMPORTANT)
#     remove_from_all_states(clinic_id, appointment_id)

#     btype = buffer_type(appointment, checkin_time)
#     if btype:
#         return add_to_buffer(appointment, clinic_id, checkin_time, btype)

#     queue_key = f"queue:{clinic_id}:{today_key()}"

#     base = 1
#     score = calculate_queue_score(base, checkin_time)

#     payload = {
#         "appointment_id": str(appointment_id),
#         "name": appointment["name"],
#         "phone": appointment["phone"],
#         "slot_at": appointment["appointment_at"].isoformat(),
#         "checkin_at": checkin_time.isoformat()
#     }

#     redis_client.zadd(queue_key, {json.dumps(payload): score})
#     return {"status": "queued", "score": score}


# # ---------------- PROMOTION LOGIC ----------------
# def promote_buffer_early(clinic_id):
#     clinic = clinics_col.find_one({"_id": clinic_id})
#     if not clinic:
#         return
#     now = datetime.now()
#     today = now.date()

#     clinic_start = datetime.strptime(
#         clinic["slot_settings"]["start"], "%H:%M"
#     ).time()

#     first_slot_at = datetime.combine(today, clinic_start)

#     buffer_key = f"buffer:early:{clinic_id}:{today_key()}"
#     queue_key = f"queue:{clinic_id}:{today_key()}"

#     items = redis_client.zrange(buffer_key, 0, -1)
#     for raw in items:
#         p = json.loads(raw)
#         slot_at = datetime.fromisoformat(p["slot_at"])
#         diff = (slot_at - now).total_seconds() / 60

#         # 🛑 FIRST SLOT RULE
#         if slot_at == first_slot_at and now < slot_at:
#             continue  # wait till clinic opens

#         if now >= slot_at or diff <= EARLY_FILL_MIN:
#             redis_client.zrem(buffer_key, raw)
#             score = calculate_queue_score(1, datetime.fromisoformat(p["checkin_at"]))
#             redis_client.zadd(queue_key, {json.dumps(p): score})


# def promote_buffer_late_if_idle(clinic_id):
#     clinic = clinics_col.find_one({"_id": clinic_id})
#     if not clinic:
#         return

#     today = today_key()
#     capacity = int(clinic["slot_settings"]["capacity_per_slot"])

#     # 1️⃣ ACTIVE must have space
#     if active_count(clinic_id) >= capacity:
#         return

#     # 2️⃣ WAITING must be empty
#     queue_key = f"queue:{clinic_id}:{today}"
#     if redis_client.zcard(queue_key) > 0:
#         return

#     # 3️⃣ UPCOMING must be empty
#     upcoming_key = f"buffer:early:{clinic_id}:{today}"
#     if redis_client.zcard(upcoming_key) > 0:
#         return

#     # 4️⃣ NOW and ONLY NOW → allow OVERFLOW
#     buffer_key = f"buffer:late:{clinic_id}:{today}"

#     # FCFS: oldest checkin first
#     items = redis_client.zrange(buffer_key, 0, -1)
#     if not items:
#         return

#     raw = items[0]  # oldest
#     p = json.loads(raw)

#     redis_client.zrem(buffer_key, raw)

#     # Use original check-in time as queue score (FCFS)
#     score = calculate_queue_score(
#     1,
#     datetime.fromisoformat(p["checkin_at"])
# )
#     redis_client.zadd(queue_key, {json.dumps(p): score})




# # ---------------- CALL NEXT ----------------
# def call_next_patient(clinic_id):
#     clinic = clinics_col.find_one({"_id": clinic_id})
#     if not clinic:
#         return None

#     lock_key = f"lock:activate:{clinic_id}:{today_key()}"
#     if not redis_client.set(lock_key, "1", nx=True, ex=3):
#         return None

#     try:
        

#         promote_buffer_early(clinic_id)
#         promote_buffer_late_if_idle(clinic_id)

#         capacity = int(clinic["slot_settings"]["capacity_per_slot"])
#         if active_count(clinic_id) >= capacity:
#             return None

#         queue_key = f"queue:{clinic_id}:{today_key()}"
#         item = redis_client.zpopmin(queue_key, 1)
#         if not item:
#             return None

#         payload = json.loads(item[0][0])
#         appointment_id = payload["appointment_id"]

#         # 🔥 ENSURE checkout token exists
#         appointment = appointments_col.find_one(
#             {"_id": ObjectId(appointment_id)}
#         )

#         if appointment and not appointment.get("checkout"):
#             checkout_token = uuid.uuid4().hex
#             appointments_col.update_one(
#                 {"_id": ObjectId(appointment_id)},
#                 {"$set": {
#                     "checkout": {
#                         "token_code": checkout_token,
#                         "qr_sent": False,
#                         "checked_out": False,
#                         "checked_out_at": None
#                     }
#                 }}
#             )

#         mark_active(clinic_id, payload)

#         return {
#             "patient": payload
#         }

#     finally:
#         redis_client.delete(lock_key)


# queue_engine.py
import json
import uuid
from datetime import datetime, timedelta

from db.redis_mongo import redis_client, appointments_col, clinics_col

QUEUE_TTL_SECONDS = 2 * 24 * 60 * 60


LUA_PROMOTE_AND_POP = """
local queue_key = KEYS[1]
local early_key = KEYS[2]
local late_key = KEYS[3]

local payload_prefix = ARGV[1]
local now_ts = tonumber(ARGV[2])
local early_fill_sec = tonumber(ARGV[3])
local capacity = tonumber(ARGV[4])
local active_count = tonumber(ARGV[5])
local first_slot_ts = tonumber(ARGV[6])
local allow_late = tonumber(ARGV[7])

-- Promote from early buffer
local early_members = redis.call('ZRANGE', early_key, 0, 50)
for i, member in ipairs(early_members) do
    local payload_key = payload_prefix .. member
    local slot_at_ts = tonumber(redis.call('HGET', payload_key, 'slot_at_ts'))
    local checkin_at_ts = tonumber(redis.call('HGET', payload_key, 'checkin_at_ts'))
    local priority_offset = tonumber(redis.call('HGET', payload_key, 'priority_offset')) or 0

    if slot_at_ts and checkin_at_ts then
        local diff = slot_at_ts - now_ts
        local is_first_slot_future = (first_slot_ts > 0 and slot_at_ts == first_slot_ts and now_ts < slot_at_ts)
        if (not is_first_slot_future) and (now_ts >= slot_at_ts or diff <= early_fill_sec) then
            redis.call('ZREM', early_key, member)
            redis.call('ZADD', queue_key, checkin_at_ts + priority_offset, member)
        end
    else
        redis.call('ZREM', early_key, member)
    end
end

-- Promote from late buffer if idle
if allow_late == 1 then
    if active_count < capacity and redis.call('ZCARD', queue_key) == 0 and redis.call('ZCARD', early_key) == 0 then
        local late_member = redis.call('ZRANGE', late_key, 0, 1)[1]
        if late_member then
            local payload_key = payload_prefix .. late_member
            local checkin_at_ts = tonumber(redis.call('HGET', payload_key, 'checkin_at_ts'))
            local priority_offset = tonumber(redis.call('HGET', payload_key, 'priority_offset')) or 0
            if checkin_at_ts then
                redis.call('ZREM', late_key, late_member)
                redis.call('ZADD', queue_key, checkin_at_ts + priority_offset, late_member)
            else
                redis.call('ZREM', late_key, late_member)
            end
        end
    end
end

if active_count >= capacity then
    return nil
end

local result = redis.call('ZPOPMIN', queue_key, 1)
if result and #result > 0 then
    return result[1]
end
return nil
"""


# ===================== HELPERS =====================

def today_key(date=None):
    return (date or datetime.now()).strftime("%Y-%m-%d")


def _queue_key(clinic_id, date_key=None):
    return f"queue:{clinic_id}:{date_key or today_key()}"


def _buffer_key(clinic_id, btype, date_key=None):
    return f"buffer:{btype}:{clinic_id}:{date_key or today_key()}"


def _payload_key(clinic_id, appointment_id, date_key=None):
    return f"queue_payload:{clinic_id}:{date_key or today_key()}:{appointment_id}"


def _store_payload(clinic_id, appointment_id, payload, date_key=None):
    key = _payload_key(clinic_id, appointment_id, date_key)
    redis_client.hset(key, mapping=payload)
    redis_client.expire(key, QUEUE_TTL_SECONDS)


def _get_payload(clinic_id, appointment_id, date_key=None):
    key = _payload_key(clinic_id, appointment_id, date_key)
    payload = redis_client.hgetall(key)
    return payload or None


def _priority_offset_seconds(appointment, slot_settings):
    if not appointment:
        return 0

    tiers = slot_settings.get("priority_tiers") or {}
    tier = appointment.get("priority_tier") or appointment.get("priority")

    if not tier and appointment.get("is_walkin"):
        tier = "walkin"

    if tier and tier in tiers:
        try:
            return int(tiers[tier])
        except Exception:
            return 0

    if appointment.get("is_walkin"):
        try:
            return int(slot_settings.get("walkin_priority_offset", 0))
        except Exception:
            return 0

    return 0


def get_slot_settings(clinic_id):
    clinic = clinics_col.find_one(
        {"_id": clinic_id},
        {"slot_settings": 1}
    )
    if not clinic:
        raise ValueError("Clinic not found")

    return clinic["slot_settings"]


def active_count(clinic_id):
    pattern = f"active:{clinic_id}:{today_key()}:*"
    count = 0
    for _ in redis_client.scan_iter(match=pattern, count=100):
        count += 1
    return count


def mark_active(clinic_id, payload):
    key = f"active:{clinic_id}:{today_key()}:{payload['appointment_id']}"
    redis_client.hset(key, mapping={
        "appointment_id": payload["appointment_id"],
        "token": payload.get("token"),
        "name": payload["name"],
        "phone": payload["phone"],
        "active_since": datetime.now().isoformat()
    })
    redis_client.expire(key, 2 * 60 * 60)


# ===================== PRIORITY =====================

def calculate_queue_score(base, checkin_time, wait_weight):
    """
    Lower score = higher priority (zpopmin).
    We use check-in timestamp as the primary sort key for strict FCFS.
    """
    return checkin_time.timestamp()


# ===================== BUFFER DECISION =====================

def buffer_type(appointment, checkin_time, slot_settings):
    appt_time = appointment["appointment_at"]

    early_fill_min = slot_settings.get("early_fill_min", 15)
    late_grace_min = slot_settings.get("late_grace_min", 7)

    late_cutoff = appt_time + timedelta(minutes=late_grace_min)

    # Walk-ins always go to late buffer
    if appointment.get("is_walkin"):
        return "late"

    if checkin_time > late_cutoff:
        return "late"

    diff_min = (appt_time - checkin_time).total_seconds() / 60

    # Early but within fill window → eligible now
    if 0 <= diff_min <= early_fill_min:
        return None

    # Too early → upcoming
    if diff_min > early_fill_min:
        return "early"

    # On-time or late within grace
    return None


# ===================== BUFFER OPS =====================

def add_to_buffer(appointment, clinic_id, checkin_time, btype):
    date_key = today_key(checkin_time)
    key = _buffer_key(clinic_id, btype, date_key)

    slot_settings = get_slot_settings(clinic_id)
    priority_offset = _priority_offset_seconds(appointment, slot_settings)

    slot_at = appointment.get("appointment_at") or checkin_time
    slot_at_ts = slot_at.timestamp()
    checkin_at_ts = checkin_time.timestamp()

    payload = {
        "appointment_id": str(appointment["_id"]),
        "token": appointment.get("checkin", {}).get("token"),
        "name": appointment["name"],
        "phone": appointment["phone"],
        "slot_at": appointment["appointment_at"].isoformat(),
        "checkin_at": checkin_time.isoformat(),
        "buffer_type": btype,
        "slot_at_ts": str(slot_at_ts),
        "checkin_at_ts": str(checkin_at_ts),
        "priority_offset": str(priority_offset),
    }

    _store_payload(clinic_id, payload["appointment_id"], payload, date_key)

    redis_client.zadd(
        key,
        {payload["appointment_id"]: checkin_time.timestamp()}
    )
    redis_client.expire(key, QUEUE_TTL_SECONDS)

    return {"status": f"buffered_{btype}"}


def remove_from_all_states(clinic_id, appointment_id):
    today = today_key()
    aid = str(appointment_id)

    for key in [
        _queue_key(clinic_id, today),
        _buffer_key(clinic_id, "early", today),
        _buffer_key(clinic_id, "late", today),
    ]:
        redis_client.zrem(key, aid)

    redis_client.delete(_payload_key(clinic_id, aid, today))


def _backfill_zset_members(clinic_id, key, slot_settings, date_key=None):
    date_key = date_key or today_key()

    for member in redis_client.zrange(key, 0, -1):
        if not member or not member.startswith("{"):
            continue

        try:
            old_payload = json.loads(member)
        except Exception:
            redis_client.zrem(key, member)
            continue

        appointment_id = old_payload.get("appointment_id")
        if not appointment_id:
            redis_client.zrem(key, member)
            continue

        appointment = appointments_col.find_one({"_id": appointment_id})

        checkin_at_raw = old_payload.get("checkin_at")
        slot_at_raw = old_payload.get("slot_at")

        try:
            checkin_at = datetime.fromisoformat(checkin_at_raw) if checkin_at_raw else datetime.now()
        except Exception:
            checkin_at = datetime.now()

        try:
            slot_at = datetime.fromisoformat(slot_at_raw) if slot_at_raw else checkin_at
        except Exception:
            slot_at = checkin_at

        priority_offset = _priority_offset_seconds(appointment, slot_settings)

        payload = {
            "appointment_id": str(appointment_id),
            "token": (appointment or {}).get("checkin", {}).get("token") if appointment else old_payload.get("token"),
            "name": (appointment or {}).get("name") if appointment else old_payload.get("name"),
            "phone": (appointment or {}).get("phone") if appointment else old_payload.get("phone"),
            "slot_at": slot_at.isoformat(),
            "checkin_at": checkin_at.isoformat(),
            "slot_at_ts": str(slot_at.timestamp()),
            "checkin_at_ts": str(checkin_at.timestamp()),
            "priority_offset": str(priority_offset),
        }

        _store_payload(clinic_id, payload["appointment_id"], payload, date_key)

        # Replace member with appointment_id, keep approximate score
        score = checkin_at.timestamp() + priority_offset
        redis_client.zrem(key, member)
        redis_client.zadd(key, {payload["appointment_id"]: score})


def backfill_queue_state(clinic_id, date_key=None):
    date_key = date_key or today_key()
    slot_settings = get_slot_settings(clinic_id)

    _backfill_zset_members(clinic_id, _queue_key(clinic_id, date_key), slot_settings, date_key)
    _backfill_zset_members(clinic_id, _buffer_key(clinic_id, "early", date_key), slot_settings, date_key)
    _backfill_zset_members(clinic_id, _buffer_key(clinic_id, "late", date_key), slot_settings, date_key)


# ===================== ADD TO QUEUE =====================

def add_to_queue(appointment_id, clinic_id, checkin_time=None):
    checkin_time = checkin_time or datetime.now()

    appointment = appointments_col.find_one({"_id": appointment_id})
    if not appointment:
        raise ValueError("Invalid appointment")

    slot_settings = get_slot_settings(clinic_id)

    # Remove duplicates
    remove_from_all_states(clinic_id, appointment_id)

    btype = buffer_type(appointment, checkin_time, slot_settings)
    if btype:
        return add_to_buffer(appointment, clinic_id, checkin_time, btype)

    priority_offset = _priority_offset_seconds(appointment, slot_settings)

    score = calculate_queue_score(
        base=1,
        checkin_time=checkin_time,
        wait_weight=slot_settings.get("wait_weight", 0.02),
    ) + priority_offset

    checkin_token = appointment.get("checkin", {}).get("token")

    slot_at = appointment.get("appointment_at") or checkin_time
    slot_at_ts = slot_at.timestamp()
    checkin_at_ts = checkin_time.timestamp()

    payload = {
        "appointment_id": str(appointment_id),
        "token": checkin_token,
        "name": appointment["name"],
        "phone": appointment["phone"],
        "slot_at": appointment["appointment_at"].isoformat(),
        "checkin_at": checkin_time.isoformat(),
        "slot_at_ts": str(slot_at_ts),
        "checkin_at_ts": str(checkin_at_ts),
        "priority_offset": str(priority_offset),
    }

    date_key = today_key(checkin_time)
    queue_key = _queue_key(clinic_id, date_key)
    _store_payload(clinic_id, payload["appointment_id"], payload, date_key)

    redis_client.zadd(queue_key, {payload["appointment_id"]: score})
    redis_client.expire(queue_key, QUEUE_TTL_SECONDS)

    return {"status": "queued", "score": score}


# ===================== PROMOTION =====================

def promote_buffer_early(clinic_id):
    slot_settings = get_slot_settings(clinic_id)

    early_fill_min = slot_settings.get("early_fill_min", 15)

    now = datetime.now()
    today = now.date()

    clinic_start = datetime.strptime(
        slot_settings["start"], "%H:%M"
    ).time()

    first_slot_at = datetime.combine(today, clinic_start)

    buffer_key = _buffer_key(clinic_id, "early")
    queue_key = _queue_key(clinic_id)

    for appointment_id in redis_client.zrange(buffer_key, 0, -1):
        p = _get_payload(clinic_id, appointment_id)
        if not p:
            redis_client.zrem(buffer_key, appointment_id)
            continue

        slot_at = datetime.fromisoformat(p["slot_at"])
        diff = (slot_at - now).total_seconds() / 60

        if slot_at == first_slot_at and now < slot_at:
            continue

        if now >= slot_at or diff <= early_fill_min:
            redis_client.zrem(buffer_key, appointment_id)
            score = float(p.get("checkin_at_ts") or datetime.fromisoformat(p["checkin_at"]).timestamp())
            priority_offset = float(p.get("priority_offset") or 0)
            redis_client.zadd(queue_key, {appointment_id: score + priority_offset})


def promote_buffer_late_if_idle(clinic_id):
    slot_settings = get_slot_settings(clinic_id)

    capacity = int(slot_settings["capacity_per_slot"])

    today = today_key()

    if active_count(clinic_id) >= capacity:
        return

    if redis_client.zcard(_queue_key(clinic_id, today)) > 0:
        return

    if redis_client.zcard(_buffer_key(clinic_id, "early", today)) > 0:
        return

    buffer_key = _buffer_key(clinic_id, "late", today)
    items = redis_client.zrange(buffer_key, 0, -1)

    if not items:
        return

    appointment_id = items[0]
    p = _get_payload(clinic_id, appointment_id, today)

    redis_client.zrem(buffer_key, appointment_id)
    if not p:
        return

    score = float(p.get("checkin_at_ts") or datetime.fromisoformat(p["checkin_at"]).timestamp())
    priority_offset = float(p.get("priority_offset") or 0)

    redis_client.zadd(
        _queue_key(clinic_id, today),
        {appointment_id: score + priority_offset}
    )


# ===================== CALL NEXT =====================

def call_next_patient(clinic_id):
    slot_settings = get_slot_settings(clinic_id)
    capacity = int(slot_settings["capacity_per_slot"])

    lock_key = f"lock:activate:{clinic_id}:{today_key()}"
    if not redis_client.set(lock_key, "1", nx=True, ex=3):
        return None

    try:
        backfill_queue_state(clinic_id)

        now_ts = datetime.now().timestamp()
        early_fill_sec = int(slot_settings.get("early_fill_min", 15)) * 60

        clinic_start = datetime.strptime(
            slot_settings["start"], "%H:%M"
        ).time()
        first_slot_at = datetime.combine(datetime.now().date(), clinic_start)
        first_slot_ts = first_slot_at.timestamp()

        appointment_id = redis_client.eval(
            LUA_PROMOTE_AND_POP,
            3,
            _queue_key(clinic_id),
            _buffer_key(clinic_id, "early"),
            _buffer_key(clinic_id, "late"),
            f"queue_payload:{clinic_id}:{today_key()}:",
            str(now_ts),
            str(early_fill_sec),
            str(capacity),
            str(active_count(clinic_id)),
            str(first_slot_ts),
            "1",
        )

        if not appointment_id:
            return None

        payload = _get_payload(clinic_id, appointment_id)
        appointment = appointments_col.find_one(
            {"_id": appointment_id}
        )

        if not payload and appointment:
            checkin_at = appointment.get("checkin", {}).get("checked_in_at") or datetime.now()
            slot_at = appointment.get("appointment_at") or checkin_at
            priority_offset = _priority_offset_seconds(appointment, slot_settings)
            payload = {
                "appointment_id": str(appointment_id),
                "token": appointment.get("checkin", {}).get("token"),
                "name": appointment["name"],
                "phone": appointment["phone"],
                "slot_at": slot_at.isoformat(),
                "checkin_at": checkin_at.isoformat(),
                "slot_at_ts": str(slot_at.timestamp()),
                "checkin_at_ts": str(checkin_at.timestamp()),
                "priority_offset": str(priority_offset),
            }
            _store_payload(clinic_id, payload["appointment_id"], payload)

        if appointment and not appointment.get("checkout"):
            appointments_col.update_one(
                {"_id": appointment_id},
                {"$set": {
                    "checkout": {
                        "token_code": uuid.uuid4().hex,
                        "qr_sent": False,
                        "checked_out": False,
                        "checked_out_at": None
                    }
                }}
            )

        if payload:
            mark_active(clinic_id, payload)

        return {"patient": payload}

    finally:
        redis_client.delete(lock_key)

