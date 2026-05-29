from uuid import UUID
from datetime import datetime, timedelta
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..db import get_db
from utils.url_utils import get_public_base_url
from utils.timezone_utils import now_in_clinic

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


def _parse_appt_date(value: str, now: datetime) -> datetime:
    try:
        return datetime.strptime(value, "%d %b").replace(year=now.year)
    except Exception:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        raise ValueError(f"unsupported date format: {value}")


@router.post("", response_model=schemas.AppointmentOut, status_code=status.HTTP_201_CREATED)
def create_appointment(payload: schemas.AppointmentCreate, db: Session = Depends(get_db)):
    return crud.create_appointment(db, payload)


@router.post("/reserve", response_model=schemas.AppointmentOut, status_code=status.HTTP_201_CREATED)
def reserve_appointment(payload: schemas.AppointmentCreate, db: Session = Depends(get_db)):
    appointment = crud.reserve_appointment(db, payload)
    if not appointment:
        raise HTTPException(status_code=409, detail="Slot is full")
    return appointment


@router.get("", response_model=list[schemas.AppointmentOut])
def list_appointments(
    clinic_id: str | None = None,
    user_id: UUID | None = None,
    phone: str | None = None,
    db: Session = Depends(get_db),
):
    return crud.list_appointments(db, clinic_id=clinic_id, user_id=user_id, phone=phone)


@router.get("/{appointment_id}", response_model=schemas.AppointmentOut)
def get_appointment(appointment_id: UUID, db: Session = Depends(get_db)):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return appointment


@router.patch("/{appointment_id}", response_model=schemas.AppointmentOut)
def update_appointment(
    appointment_id: UUID,
    payload: schemas.AppointmentUpdate,
    db: Session = Depends(get_db),
):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return crud.update_appointment(db, appointment, payload)


@router.post("/{appointment_id}/send-qr")
def send_checkin_qr(appointment_id: UUID, db: Session = Depends(get_db)):
    try:
        appointment = crud.get_appointment(db, appointment_id)
        if not appointment:
            raise HTTPException(status_code=404, detail="Appointment not found")

        clinic = crud.get_clinic(db, appointment.clinic_id)
        if not clinic:
            raise HTTPException(status_code=404, detail="Clinic not found")

        if not appointment.date or not appointment.time:
            raise HTTPException(status_code=400, detail="Appointment date/time missing")

        clinic_dict = {"_id": clinic.id, "slot_settings": clinic.slot_settings or {}}
        now = now_in_clinic(clinic_dict).replace(tzinfo=None)

        if appointment.appointment_at is None and appointment.date and appointment.time:
            appt_date = _parse_appt_date(appointment.date, now)
            try:
                appt_time = datetime.strptime(appointment.time, "%I:%M %p").time()
            except Exception:
                appt_time = datetime.strptime(appointment.time, "%H:%M").time()
            appointment_at = datetime.combine(appt_date.date(), appt_time)
            appointment = crud.update_appointment(
                db,
                appointment,
                schemas.AppointmentUpdate(appointment_at=appointment_at),
            )

        start_str = (clinic.slot_settings or {}).get("start", "00:00")
        end_str = (clinic.slot_settings or {}).get("end", "23:59")

        try:
            start_time = datetime.strptime(start_str, "%H:%M").time()
        except Exception:
            start_time = datetime.strptime(start_str, "%I:%M %p").time()

        try:
            end_time = datetime.strptime(end_str, "%H:%M").time()
        except Exception:
            end_time = datetime.strptime(end_str, "%I:%M %p").time()

        appt_date = _parse_appt_date(appointment.date, now)
        clinic_open_at = datetime.combine(appt_date.date(), start_time)
        clinic_close_at = datetime.combine(appt_date.date(), end_time)

        early_window = (clinic.slot_settings or {}).get("early_window_minutes", 45)
        closing_buffer = (clinic.slot_settings or {}).get("closing_buffer_minutes", 30)

        appointment_at = appointment.appointment_at or clinic_open_at
        valid_from = max(appointment_at - timedelta(minutes=early_window), clinic_open_at)
        valid_until = clinic_close_at - timedelta(minutes=closing_buffer)

        checkin_token = uuid.uuid4().hex
        checkin = crud.ensure_checkin(db, appointment)
        crud.update_checkin(
            db,
            checkin,
            schemas.AppointmentCheckinUpdate(
                token_code=checkin_token,
                valid_from=valid_from,
                valid_until=valid_until,
                checked_in=False,
                checked_in_at=None,
                token=None,
            ),
        )

        base_url = get_public_base_url()
        checkin_url = f"{base_url}/checkin?apt_id={appointment.id}&code={checkin_token}"

        return {"status": "sent", "appointment_id": str(appointment.id), "checkin_url": checkin_url}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"send-qr failed: {error}")
