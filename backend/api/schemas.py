from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID
from pydantic import BaseModel, Field


class ClinicBase(BaseModel):
    name: str
    owner_name: Optional[str] = None
    phone_number: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    clinic_type: Optional[str] = None
    services: List[str] = Field(default_factory=list)
    departments: List[str] = Field(default_factory=list)
    slot_settings: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class ClinicCreate(ClinicBase):
    id: str


class ClinicUpdate(BaseModel):
    name: Optional[str] = None
    owner_name: Optional[str] = None
    phone_number: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    clinic_type: Optional[str] = None
    services: Optional[List[str]] = None
    departments: Optional[List[str]] = None
    slot_settings: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class ClinicTimezoneUpdate(BaseModel):
    timezone: str


class ClinicOut(ClinicBase):
    id: str
    created_at: datetime

    class Config:
        from_attributes = True


class ClinicWhatsAppCredentialIn(BaseModel):
    phone_number_id: str
    access_token: str
    waba_id: Optional[str] = None


class ClinicWhatsAppCredentialOut(ClinicWhatsAppCredentialIn):
    id: int
    clinic_id: str

    class Config:
        from_attributes = True


class UserBase(BaseModel):
    phone: str
    name: Optional[str] = None


class UserCreate(UserBase):
    unique_id: Optional[str] = None


class UserUpdate(BaseModel):
    phone: Optional[str] = None
    name: Optional[str] = None
    unique_id: Optional[str] = None


class UserOut(UserBase):
    id: UUID
    unique_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserClinicBase(BaseModel):
    clinic_id: str
    visit_count: int = 0
    preferences: Dict[str, Any] = Field(default_factory=dict)
    crm_info: Dict[str, Any] = Field(default_factory=dict)
    last_draft: Dict[str, Any] = Field(default_factory=dict)


class UserClinicCreate(UserClinicBase):
    user_id: UUID


class UserClinicUpdate(BaseModel):
    clinic_id: Optional[str] = None
    visit_count: Optional[int] = None
    preferences: Optional[Dict[str, Any]] = None
    crm_info: Optional[Dict[str, Any]] = None
    last_draft: Optional[Dict[str, Any]] = None


class UserClinicOut(UserClinicBase):
    id: int
    user_id: UUID
    first_seen: datetime
    last_seen: datetime

    class Config:
        from_attributes = True


class UserClinicHistoryAppend(BaseModel):
    item: Dict[str, Any]


class AppointmentBase(BaseModel):
    clinic_id: str
    user_id: Optional[UUID] = None
    phone: str
    name: Optional[str] = None
    age: Optional[str] = None
    department: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    status: str = "confirmed"
    appointment_at: Optional[datetime] = None
    is_walkin: bool = False


class AppointmentCreate(AppointmentBase):
    pass


class AppointmentUpdate(BaseModel):
    clinic_id: Optional[str] = None
    user_id: Optional[UUID] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    age: Optional[str] = None
    department: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    status: Optional[str] = None
    appointment_at: Optional[datetime] = None
    is_walkin: Optional[bool] = None


class AppointmentOut(AppointmentBase):
    id: UUID
    created_at: datetime

    class Config:
        from_attributes = True


class AppointmentCheckinBase(BaseModel):
    token_code: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    checked_in: bool = False
    checked_in_at: Optional[datetime] = None
    token: Optional[str] = None


class AppointmentCheckinOut(AppointmentCheckinBase):
    id: int
    appointment_id: UUID

    class Config:
        from_attributes = True


class AppointmentCheckoutBase(BaseModel):
    token_code: Optional[str] = None
    checked_out: bool = False
    checked_out_at: Optional[datetime] = None
    qr_sent: bool = False


class AppointmentCheckoutOut(AppointmentCheckoutBase):
    id: int
    appointment_id: UUID

    class Config:
        from_attributes = True


class AppointmentCheckinUpdate(BaseModel):
    token_code: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    checked_in: Optional[bool] = None
    checked_in_at: Optional[datetime] = None
    token: Optional[str] = None


class AppointmentCheckoutUpdate(BaseModel):
    token_code: Optional[str] = None
    checked_out: Optional[bool] = None
    checked_out_at: Optional[datetime] = None
    qr_sent: Optional[bool] = None
