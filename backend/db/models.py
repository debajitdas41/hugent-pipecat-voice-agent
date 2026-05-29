import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from .pg import Base


class Clinic(Base):
    __tablename__ = "clinics"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_name = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)
    whatsapp_phone_number_id = Column(String, nullable=True, index=True)
    clinic_type = Column(String, nullable=True)
    services = Column(JSONB, nullable=False, default=list)
    departments = Column(JSONB, nullable=False, default=list)
    slot_settings = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    whatsapp_credentials = relationship(
        "ClinicWhatsAppCredential",
        back_populates="clinic",
        uselist=False,
        cascade="all, delete-orphan",
    )
    user_links = relationship("UserClinic", back_populates="clinic")
    appointments = relationship("Appointment", back_populates="clinic")


class ClinicWhatsAppCredential(Base):
    __tablename__ = "clinic_whatsapp_credentials"

    id = Column(Integer, primary_key=True)
    clinic_id = Column(String, ForeignKey("clinics.id"), nullable=False, unique=True)
    phone_number_id = Column(String, nullable=False)
    access_token = Column(Text, nullable=False)
    waba_id = Column(String, nullable=True)

    clinic = relationship("Clinic", back_populates="whatsapp_credentials")


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone = Column(String, unique=True, index=True, nullable=False)
    unique_id = Column(String, nullable=True, index=True)
    name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    clinics = relationship("UserClinic", back_populates="user")
    appointments = relationship("Appointment", back_populates="user")


class UserClinic(Base):
    __tablename__ = "user_clinic"
    __table_args__ = (
        UniqueConstraint("user_id", "clinic_id", name="uq_user_clinic"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    clinic_id = Column(String, ForeignKey("clinics.id"), nullable=False)
    first_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime, default=datetime.utcnow, nullable=False)
    visit_count = Column(Integer, default=0, nullable=False)
    preferences = Column(JSONB, nullable=False, default=dict)
    crm_info = Column(JSONB, nullable=False, default=dict)
    history = Column(JSONB, nullable=False, default=list)
    last_draft = Column(JSONB, nullable=False, default=dict)

    user = relationship("User", back_populates="clinics")
    clinic = relationship("Clinic", back_populates="user_links")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clinic_id = Column(String, ForeignKey("clinics.id"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)

    phone = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    age = Column(String, nullable=True)
    department = Column(String, nullable=True)
    date = Column(String, nullable=True)
    time = Column(String, nullable=True)
    status = Column(String, nullable=False, default="confirmed")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    appointment_at = Column(DateTime, nullable=True)
    is_walkin = Column(Boolean, default=False, nullable=False)

    clinic = relationship("Clinic", back_populates="appointments")
    user = relationship("User", back_populates="appointments")
    checkin = relationship(
        "AppointmentCheckin",
        back_populates="appointment",
        uselist=False,
        cascade="all, delete-orphan",
    )
    checkout = relationship(
        "AppointmentCheckout",
        back_populates="appointment",
        uselist=False,
        cascade="all, delete-orphan",
    )


class AppointmentCheckin(Base):
    __tablename__ = "appointment_checkins"

    id = Column(Integer, primary_key=True)
    appointment_id = Column(UUID(as_uuid=True), ForeignKey("appointments.id"), unique=True, nullable=False)
    token_code = Column(String, nullable=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    checked_in = Column(Boolean, default=False, nullable=False)
    checked_in_at = Column(DateTime, nullable=True)
    token = Column(String, nullable=True)

    appointment = relationship("Appointment", back_populates="checkin")


class AppointmentCheckout(Base):
    __tablename__ = "appointment_checkouts"

    id = Column(Integer, primary_key=True)
    appointment_id = Column(UUID(as_uuid=True), ForeignKey("appointments.id"), unique=True, nullable=False)
    token_code = Column(String, nullable=True)
    checked_out = Column(Boolean, default=False, nullable=False)
    checked_out_at = Column(DateTime, nullable=True)
    qr_sent = Column(Boolean, default=False, nullable=False)

    appointment = relationship("Appointment", back_populates="checkout")
