from datetime import datetime
import hashlib
import struct

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from db.models import (
    Appointment,
    AppointmentCheckin,
    AppointmentCheckout,
    Clinic,
    ClinicWhatsAppCredential,
    User,
    UserClinic,
)
from . import schemas


# -------- Clinics --------

def create_clinic(db: Session, payload: schemas.ClinicCreate) -> Clinic:
    clinic = Clinic(**payload.dict())
    db.add(clinic)
    db.commit()
    db.refresh(clinic)
    return clinic


def list_clinics(db: Session) -> list[Clinic]:
    return list(db.execute(select(Clinic)).scalars().all())


def get_clinic(db: Session, clinic_id: str) -> Clinic | None:
    return db.get(Clinic, clinic_id)


def get_clinic_by_phone_number_id(db: Session, phone_number_id: str) -> Clinic | None:
    stmt = select(Clinic).where(Clinic.whatsapp_phone_number_id == phone_number_id)
    return db.execute(stmt).scalar_one_or_none()


def update_clinic(db: Session, clinic: Clinic, payload: schemas.ClinicUpdate) -> Clinic:
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(clinic, key, value)
    db.commit()
    db.refresh(clinic)
    return clinic


# -------- WhatsApp Credentials --------

def upsert_whatsapp_credentials(
    db: Session,
    clinic: Clinic,
    payload: schemas.ClinicWhatsAppCredentialIn,
) -> ClinicWhatsAppCredential:
    existing = clinic.whatsapp_credentials
    if existing:
        for key, value in payload.dict().items():
            setattr(existing, key, value)
        db.commit()
        db.refresh(existing)
        return existing

    creds = ClinicWhatsAppCredential(clinic_id=clinic.id, **payload.dict())
    db.add(creds)
    db.commit()
    db.refresh(creds)
    return creds


# -------- Users --------

def create_user(db: Session, payload: schemas.UserCreate) -> User:
    user = User(**payload.dict())
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_phone(db: Session, phone: str) -> User | None:
    stmt = select(User).where(User.phone == phone)
    return db.execute(stmt).scalar_one_or_none()


def get_user(db: Session, user_id) -> User | None:
    return db.get(User, user_id)


def update_user(db: Session, user: User, payload: schemas.UserUpdate) -> User:
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return user


# -------- User Clinic --------

def get_user_clinic(db: Session, user_id, clinic_id: str) -> UserClinic | None:
    stmt = (
        select(UserClinic)
        .where(UserClinic.user_id == user_id)
        .where(UserClinic.clinic_id == clinic_id)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_user_clinic_by_id(db: Session, link_id) -> UserClinic | None:
    return db.get(UserClinic, link_id)


def create_user_clinic(db: Session, payload: schemas.UserClinicCreate) -> UserClinic:
    link = UserClinic(**payload.dict())
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def touch_user_clinic(db: Session, link: UserClinic) -> UserClinic:
    link.last_seen = datetime.utcnow()
    link.visit_count += 1
    db.commit()
    db.refresh(link)
    return link


def update_user_clinic(db: Session, link: UserClinic, payload: schemas.UserClinicUpdate) -> UserClinic:
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(link, key, value)
    db.commit()
    db.refresh(link)
    return link


def append_user_clinic_history(db: Session, link: UserClinic, item: dict) -> UserClinic:
    history = list(link.history or [])
    history.append(item)
    link.history = history
    db.commit()
    db.refresh(link)
    return link


# -------- Appointments --------

def create_appointment(db: Session, payload: schemas.AppointmentCreate) -> Appointment:
    appointment = Appointment(**payload.dict())
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    return appointment


def get_appointment(db: Session, appointment_id) -> Appointment | None:
    return db.get(Appointment, appointment_id)


def list_appointments(db: Session, clinic_id: str | None = None, user_id=None, phone: str | None = None) -> list[Appointment]:
    stmt = select(Appointment)
    if clinic_id:
        stmt = stmt.where(Appointment.clinic_id == clinic_id)
    if user_id:
        stmt = stmt.where(Appointment.user_id == user_id)
    if phone:
        stmt = stmt.where(Appointment.phone == phone)
    return list(db.execute(stmt).scalars().all())


def update_appointment(db: Session, appointment: Appointment, payload: schemas.AppointmentUpdate) -> Appointment:
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(appointment, key, value)
    db.commit()
    db.refresh(appointment)
    return appointment


def count_appointments_for_slot(db: Session, clinic_id: str, date: str, time: str) -> int:
    stmt = select(func.count()).select_from(Appointment).where(
        Appointment.clinic_id == clinic_id,
        Appointment.date == date,
        Appointment.time == time,
    )
    return int(db.execute(stmt).scalar() or 0)


def reserve_appointment(db: Session, payload: schemas.AppointmentCreate) -> Appointment | None:
    clinic = get_clinic(db, payload.clinic_id)
    if not clinic:
        return None

    capacity = int((clinic.slot_settings or {}).get("capacity_per_slot", 1))

    lock_key_raw = f"{payload.clinic_id}|{payload.date}|{payload.time}".encode("utf-8")
    lock_key = struct.unpack("!q", hashlib.sha256(lock_key_raw).digest()[:8])[0]
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    booked = count_appointments_for_slot(db, payload.clinic_id, payload.date, payload.time)
    if booked >= capacity:
        return None

    appointment = Appointment(**payload.dict())
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    return appointment


# -------- Check-in/out --------

def ensure_checkin(db: Session, appointment: Appointment) -> AppointmentCheckin:
    if appointment.checkin:
        return appointment.checkin
    checkin = AppointmentCheckin(appointment_id=appointment.id)
    db.add(checkin)
    db.commit()
    db.refresh(checkin)
    return checkin


def ensure_checkout(db: Session, appointment: Appointment) -> AppointmentCheckout:
    if appointment.checkout:
        return appointment.checkout
    checkout = AppointmentCheckout(appointment_id=appointment.id)
    db.add(checkout)
    db.commit()
    db.refresh(checkout)
    return checkout


def update_checkin(
    db: Session,
    checkin: AppointmentCheckin,
    payload: schemas.AppointmentCheckinUpdate,
) -> AppointmentCheckin:
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(checkin, key, value)
    db.commit()
    db.refresh(checkin)
    return checkin


def update_checkout(
    db: Session,
    checkout: AppointmentCheckout,
    payload: schemas.AppointmentCheckoutUpdate,
) -> AppointmentCheckout:
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        setattr(checkout, key, value)
    db.commit()
    db.refresh(checkout)
    return checkout
