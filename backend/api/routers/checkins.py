from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..db import get_db

router = APIRouter(prefix="/api/appointments", tags=["checkins"])


@router.get("/{appointment_id}/checkin", response_model=schemas.AppointmentCheckinOut)
def get_checkin(appointment_id: UUID, db: Session = Depends(get_db)):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if not appointment.checkin:
        raise HTTPException(status_code=404, detail="Check-in not found")
    return appointment.checkin


@router.post("/{appointment_id}/checkin", response_model=schemas.AppointmentCheckinOut)
def ensure_checkin(appointment_id: UUID, db: Session = Depends(get_db)):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return crud.ensure_checkin(db, appointment)


@router.patch("/{appointment_id}/checkin", response_model=schemas.AppointmentCheckinOut)
def update_checkin(
    appointment_id: UUID,
    payload: schemas.AppointmentCheckinUpdate,
    db: Session = Depends(get_db),
):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    checkin = crud.ensure_checkin(db, appointment)
    return crud.update_checkin(db, checkin, payload)
