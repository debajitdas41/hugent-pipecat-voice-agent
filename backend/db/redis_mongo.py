import os
import time
import uuid
from datetime import datetime, timezone

import redis
from dotenv import load_dotenv
from sqlalchemy import select

from .pg import SessionLocal
from .models import (
    Appointment,
    AppointmentCheckin,
    AppointmentCheckout,
    Clinic,
    ClinicWhatsAppCredential,
    User,
    UserClinic,
)

# Load env
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# -------------------------
# 🔴 Redis Setup
# -------------------------
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD,
    decode_responses=True,
)

# -------------------------
# TTL CONFIG
# -------------------------
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS"))
SEEN_TTL_SECONDS = int(os.getenv("SEEN_TTL_SECONDS"))


def _clinic_to_dict(clinic: Clinic) -> dict:
    creds = None
    if clinic.whatsapp_credentials:
        creds = {
            "phone_number_id": clinic.whatsapp_credentials.phone_number_id,
            "access_token": clinic.whatsapp_credentials.access_token,
            "waba_id": clinic.whatsapp_credentials.waba_id,
        }

    return {
        "_id": clinic.id,
        "name": clinic.name,
        "owner_name": clinic.owner_name,
        "phone_number": clinic.phone_number,
        "whatsapp_phone_number_id": clinic.whatsapp_phone_number_id,
        "clinic_type": clinic.clinic_type,
        "services": clinic.services or [],
        "departments": clinic.departments or [],
        "slot_settings": clinic.slot_settings or {},
        "created_at": clinic.created_at,
        "is_active": clinic.is_active,
        "whatsapp_credentials": creds,
    }


def _user_link_to_dict(user: User, link: UserClinic) -> dict:
    return {
        "_id": str(user.id),
        "phone": user.phone,
        "clinic_id": link.clinic_id,
        "unique_id": user.unique_id,
        "name": user.name,
        "first_seen": link.first_seen,
        "last_seen": link.last_seen,
        "visit_count": link.visit_count,
        "preferences": link.preferences or {},
        "crm_info": link.crm_info or {},
        "history": link.history or [],
        "last_draft": link.last_draft or {},
    }


def _appointment_to_dict(appointment: Appointment) -> dict:
    checkin = None
    if appointment.checkin:
        checkin = {
            "token_code": appointment.checkin.token_code,
            "valid_from": appointment.checkin.valid_from,
            "valid_until": appointment.checkin.valid_until,
            "checked_in": appointment.checkin.checked_in,
            "checked_in_at": appointment.checkin.checked_in_at,
            "token": appointment.checkin.token,
        }

    checkout = None
    if appointment.checkout:
        checkout = {
            "token_code": appointment.checkout.token_code,
            "checked_out": appointment.checkout.checked_out,
            "checked_out_at": appointment.checkout.checked_out_at,
            "qr_sent": appointment.checkout.qr_sent,
        }

    return {
        "_id": str(appointment.id),
        "clinic_id": appointment.clinic_id,
        "user_id": str(appointment.user_id) if appointment.user_id else None,
        "phone": appointment.phone,
        "name": appointment.name,
        "age": appointment.age,
        "department": appointment.department,
        "date": appointment.date,
        "time": appointment.time,
        "status": appointment.status,
        "created_at": appointment.created_at,
        "appointment_at": appointment.appointment_at,
        "is_walkin": appointment.is_walkin,
        "checkin": checkin,
        "checkout": checkout,
    }


class _InsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class ClinicsCollection:
    def find_one(self, filter_dict: dict, projection: dict | None = None):
        with SessionLocal() as db:
            if "_id" in filter_dict:
                clinic = db.get(Clinic, filter_dict["_id"])
            elif "whatsapp_phone_number_id" in filter_dict:
                stmt = select(Clinic).where(
                    Clinic.whatsapp_phone_number_id == filter_dict["whatsapp_phone_number_id"]
                )
                clinic = db.execute(stmt).scalar_one_or_none()
            else:
                return None

            if not clinic:
                return None

            if clinic.whatsapp_credentials is None:
                creds_stmt = select(ClinicWhatsAppCredential).where(
                    ClinicWhatsAppCredential.clinic_id == clinic.id
                )
                clinic.whatsapp_credentials = db.execute(creds_stmt).scalar_one_or_none()

            if projection and projection.get("_id") and len(projection.keys()) == 1:
                return {"_id": clinic.id}

            if projection and projection.get("slot_settings") and len(projection.keys()) == 1:
                return {"slot_settings": clinic.slot_settings or {}}

            return _clinic_to_dict(clinic)

    def find(self, filter_dict: dict | None = None, projection: dict | None = None):
        with SessionLocal() as db:
            stmt = select(Clinic)
            clinics = db.execute(stmt).scalars().all()

        results = []
        for clinic in clinics:
            if projection and projection.get("_id") and len(projection.keys()) == 1:
                results.append({"_id": clinic.id})
            else:
                results.append(_clinic_to_dict(clinic))
        return results

    def update_one(self, filter_dict: dict, update_doc: dict):
        with SessionLocal() as db:
            clinic = db.get(Clinic, filter_dict.get("_id"))
            if not clinic:
                return
            for key, value in update_doc.get("$set", {}).items():
                setattr(clinic, key, value)
            db.commit()


class UsersCollection:
    def _get_user_and_link(self, db, phone: str, clinic_id: str):
        stmt = select(User).where(User.phone == phone)
        user = db.execute(stmt).scalar_one_or_none()
        if not user:
            return None, None

        link_stmt = (
            select(UserClinic)
            .where(UserClinic.user_id == user.id)
            .where(UserClinic.clinic_id == clinic_id)
        )
        link = db.execute(link_stmt).scalar_one_or_none()
        return user, link

    def find_one(self, filter_dict: dict):
        phone = filter_dict.get("phone")
        clinic_id = filter_dict.get("clinic_id")
        if not phone or not clinic_id:
            return None

        with SessionLocal() as db:
            user, link = self._get_user_and_link(db, phone, clinic_id)
            if not user or not link:
                return None
            return _user_link_to_dict(user, link)

    def insert_one(self, doc: dict):
        phone = doc.get("phone")
        clinic_id = doc.get("clinic_id")
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            stmt = select(User).where(User.phone == phone)
            user = db.execute(stmt).scalar_one_or_none()

            if not user:
                unique_id = doc.get("unique_id") or f"U{int(datetime.timestamp(now))}{phone[-4:]}"
                user = User(
                    phone=phone,
                    unique_id=unique_id,
                    name=doc.get("name"),
                    created_at=now,
                )
                db.add(user)
                db.flush()

            link_stmt = (
                select(UserClinic)
                .where(UserClinic.user_id == user.id)
                .where(UserClinic.clinic_id == clinic_id)
            )
            link = db.execute(link_stmt).scalar_one_or_none()
            if not link:
                link = UserClinic(
                    user_id=user.id,
                    clinic_id=clinic_id,
                    first_seen=doc.get("first_seen", now),
                    last_seen=doc.get("last_seen", now),
                    visit_count=doc.get("visit_count", 0),
                    preferences=doc.get("preferences", {}),
                    crm_info=doc.get("crm_info", {}),
                    history=doc.get("history", []),
                    last_draft=doc.get("last_draft", {}),
                )
                db.add(link)

            db.commit()
            return _InsertResult(inserted_id=str(user.id))

    def update_one(self, filter_dict: dict, update_doc: dict):
        phone = filter_dict.get("phone")
        clinic_id = filter_dict.get("clinic_id")
        if not phone or not clinic_id:
            return

        with SessionLocal() as db:
            user, link = self._get_user_and_link(db, phone, clinic_id)
            if not user or not link:
                return

            for key, value in update_doc.get("$set", {}).items():
                if key.startswith("last_draft."):
                    field = key.split(".", 1)[1]
                    draft = dict(link.last_draft or {})
                    if isinstance(value, datetime):
                        value = value.isoformat()
                    draft[field] = value
                    link.last_draft = draft
                elif key == "last_draft":
                    if isinstance(value, dict):
                        value = {
                            k: (v.isoformat() if isinstance(v, datetime) else v)
                            for k, v in value.items()
                        }
                    link.last_draft = value
                elif key == "last_seen":
                    link.last_seen = value
                elif key == "name":
                    user.name = value
                else:
                    setattr(link, key, value)

            for key, value in update_doc.get("$inc", {}).items():
                if hasattr(link, key):
                    setattr(link, key, (getattr(link, key) or 0) + value)

            for key, value in update_doc.get("$push", {}).items():
                if key == "history":
                    history = list(link.history or [])
                    history.append(value)
                    link.history = history

            db.commit()


class AppointmentsCollection:
    def insert_one(self, doc: dict):
        with SessionLocal() as db:
            appointment = Appointment(
                clinic_id=doc.get("clinic_id"),
                user_id=doc.get("user_id"),
                phone=doc.get("phone"),
                name=doc.get("name"),
                age=doc.get("age"),
                department=doc.get("department"),
                date=doc.get("date"),
                time=doc.get("time"),
                status=doc.get("status", "confirmed"),
                created_at=doc.get("created_at") or datetime.utcnow(),
                appointment_at=doc.get("appointment_at"),
                is_walkin=doc.get("is_walkin", False),
            )
            db.add(appointment)
            db.flush()

            checkin_doc = doc.get("checkin")
            if checkin_doc:
                db.add(
                    AppointmentCheckin(
                        appointment_id=appointment.id,
                        token_code=checkin_doc.get("token_code"),
                        valid_from=checkin_doc.get("valid_from"),
                        valid_until=checkin_doc.get("valid_until"),
                        checked_in=checkin_doc.get("checked_in", False),
                        checked_in_at=checkin_doc.get("checked_in_at"),
                        token=checkin_doc.get("token"),
                    )
                )

            checkout_doc = doc.get("checkout")
            if checkout_doc:
                db.add(
                    AppointmentCheckout(
                        appointment_id=appointment.id,
                        token_code=checkout_doc.get("token_code"),
                        checked_out=checkout_doc.get("checked_out", False),
                        checked_out_at=checkout_doc.get("checked_out_at"),
                        qr_sent=checkout_doc.get("qr_sent", False),
                    )
                )

            db.commit()
            return _InsertResult(inserted_id=str(appointment.id))

    def find_one(self, filter_dict: dict):
        appointment_id = filter_dict.get("_id")
        if appointment_id is None:
            return None
        appointment_id = str(appointment_id)

        with SessionLocal() as db:
            appointment = db.get(Appointment, appointment_id)
            if not appointment:
                return None

            if appointment.checkin is None:
                appointment.checkin = db.execute(
                    select(AppointmentCheckin).where(
                        AppointmentCheckin.appointment_id == appointment.id
                    )
                ).scalar_one_or_none()

            if appointment.checkout is None:
                appointment.checkout = db.execute(
                    select(AppointmentCheckout).where(
                        AppointmentCheckout.appointment_id == appointment.id
                    )
                ).scalar_one_or_none()

            return _appointment_to_dict(appointment)

    def update_one(self, filter_dict: dict, update_doc: dict):
        appointment_id = filter_dict.get("_id")
        if appointment_id is None:
            return
        appointment_id = str(appointment_id)

        with SessionLocal() as db:
            appointment = db.get(Appointment, appointment_id)
            if not appointment:
                return

            checkin = appointment.checkin or db.execute(
                select(AppointmentCheckin).where(
                    AppointmentCheckin.appointment_id == appointment.id
                )
            ).scalar_one_or_none()

            checkout = appointment.checkout or db.execute(
                select(AppointmentCheckout).where(
                    AppointmentCheckout.appointment_id == appointment.id
                )
            ).scalar_one_or_none()

            for key, value in update_doc.get("$set", {}).items():
                if key.startswith("checkin."):
                    if not checkin:
                        checkin = AppointmentCheckin(appointment_id=appointment.id)
                        db.add(checkin)
                    setattr(checkin, key.split(".", 1)[1], value)
                elif key.startswith("checkout."):
                    if not checkout:
                        checkout = AppointmentCheckout(appointment_id=appointment.id)
                        db.add(checkout)
                    setattr(checkout, key.split(".", 1)[1], value)
                else:
                    setattr(appointment, key, value)

            db.commit()

    def count_documents(self, filter_dict: dict) -> int:
        with SessionLocal() as db:
            stmt = select(Appointment)
            if "clinic_id" in filter_dict:
                stmt = stmt.where(Appointment.clinic_id == filter_dict["clinic_id"])
            if "date" in filter_dict:
                stmt = stmt.where(Appointment.date == filter_dict["date"])
            if "time" in filter_dict:
                stmt = stmt.where(Appointment.time == filter_dict["time"])
            return len(db.execute(stmt).scalars().all())


users_col = UsersCollection()
clinics_col = ClinicsCollection()
appointments_col = AppointmentsCollection()


# -----------------------------------------
# 🔷 USER MANAGEMENT (Postgres)
# -----------------------------------------
def get_user(phone, clinic_id):
    return users_col.find_one({"phone": phone, "clinic_id": clinic_id})


def upsert_user_if_not_exists(phone, clinic_id, name=None):
    existing = users_col.find_one({"phone": phone, "clinic_id": clinic_id})
    if existing:
        return existing

    now = datetime.now(timezone.utc)
    user_data = {
        "phone": phone,
        "clinic_id": clinic_id,
        "unique_id": f"U{int(datetime.timestamp(now))}{phone[-4:]}",
        "name": name,
        "first_seen": now,
        "last_seen": now,
        "visit_count": 1,
        "preferences": {},
        "crm_info": {},
        "history": [],
        "last_draft": {},
    }
    users_col.insert_one(user_data)
    return users_col.find_one({"phone": phone, "clinic_id": clinic_id})


def update_user_activity(phone, clinic_id):
    now = datetime.now(timezone.utc)
    users_col.update_one(
        {"phone": phone, "clinic_id": clinic_id},
        {"$set": {"last_seen": now}, "$inc": {"visit_count": 1}},
    )



# ------------------------------------------------
# 🔶 SESSION MANAGEMENT (Redis)
# ------------------------------------------------
def create_session(phone, clinic_id, ttl: int = None):
    if ttl is None:
        ttl = SESSION_TTL_SECONDS
    key = f"session:{clinic_id}:{phone}"
    session_data = {
        "current_flow": "",
        "step": "start",
        "last_step_time": str(time.time()),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    print("\n[CREATE_SESSION] Creating session:", key)
    print("create_session")
    try:
        # 1️⃣ Run HSET and capture return value
        result = redis_client.hset(key, mapping=session_data)
        print("[DEBUG] HSET result:", result)

        # 2️⃣ Check immediately if key exists
        exists = redis_client.exists(key)
        print("[DEBUG] Exists right after HSET:", exists)

        # 3️⃣ Read full hash back (proof of writing)
        read_back = redis_client.hgetall(key)
        print("[DEBUG] Hash stored:", read_back)

        # 4️⃣ Set TTL
        redis_client.expire(key, ttl)

        # 5️⃣ Print TTL immediately
        ttl_now = redis_client.ttl(key)
        print("[DEBUG] TTL immediately:", ttl_now)

        print("[DEBUG] Session create completed.\n")
        
    except Exception as e:
        print(f"[ERROR] redis hset/expire failed for {key}: {e}")



def get_session(phone, clinic_id):
    """
    Returns session dict or None.
    NEVER use redis.exists — use hgetall safely.
    """
    key = f"session:{clinic_id}:{phone}"
    data = redis_client.hgetall(key)
    ttl = redis_client.ttl(key)
    print(f"[GET_SESSION] key={key} pid={os.getpid()} conn={redis_client.connection_pool.connection_kwargs} ttl={ttl} data={data}")
    print("get_session")
    if data:
        return data
    return None


def update_session(phone, clinic_id, data: dict, ttl: int = None):
    """
    Safely updates session fields.
    """
    if ttl is None:
        ttl = SESSION_TTL_SECONDS

    key = f"session:{clinic_id}:{phone}"
    redis_client.hset(key, mapping=data)
    redis_client.expire(key, ttl)
    print("update_session")

# ============================================
# NEW EXPIRATION FLAG FOR 45–60 SEC WINDOW
# ============================================

# in redis_mongo.py (or wherever you keep these helpers)

def mark_session_expired(phone, clinic_id, ttl=None):
    """
    Mark that a session just expired. The TTL of this flag must equal the
    *expired window* = (SEEN_TTL_SECONDS - SESSION_TTL_SECONDS).
    If that difference is <= 0 we default to a safe small window (1s).
    """
    if ttl is None:
        # compute the expired-window duration (seconds)
        window = SEEN_TTL_SECONDS - SESSION_TTL_SECONDS
        print("Computed expired window:", window)
        if window <= 0:
            # defensive: if config is weird, give a tiny window instead of non-positive TTL
            window = 1
        ttl = window
        print("mark_session_expired")        

    try:
        redis_client.set(f"expired_flag:{clinic_id}:{phone}", "1", ex=ttl)
        # debug:
        print(f"[DEBUG] set expired_flag:{clinic_id}:{phone} ex={ttl}")
    except Exception as e:
        print(f"[ERROR] mark_session_expired failed for {clinic_id}:{phone}: {e}")


def has_expired_flag(phone, clinic_id):
    """Return True if the expired_flag exists (user is inside expired window)."""
    try:
        return redis_client.exists(f"expired_flag:{clinic_id}:{phone}") == 1
        print("has_expired_flag")
    except Exception as e:
        print(f"[ERROR] has_expired_flag error for {clinic_id}:{phone}: {e}")
        return False



# ------------------------------------------------
# 🔵 USER VISIT MARKER (Redis)
# ------------------------------------------------
def mark_user_seen_in_redis(phone, clinic_id, ttl_seconds: int = None):
    if ttl_seconds is None:
        ttl_seconds = SEEN_TTL_SECONDS
    redis_client.set(f"seen:{clinic_id}:{phone}", "1", ex=ttl_seconds)
    print("mark_user_seen_in_redis")


def has_seen_before(phone, clinic_id):
    # print("has_seen_before")
    return redis_client.exists(f"seen:{clinic_id}:{phone}") == 1
