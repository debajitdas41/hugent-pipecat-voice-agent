from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..db import get_db

router = APIRouter(prefix="/api/appointments", tags=["checkouts"])


@router.get("/{appointment_id}/checkout", response_model=schemas.AppointmentCheckoutOut)
def get_checkout(appointment_id: UUID, db: Session = Depends(get_db)):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if not appointment.checkout:
        raise HTTPException(status_code=404, detail="Checkout not found")
    return appointment.checkout


@router.post("/{appointment_id}/checkout", response_model=schemas.AppointmentCheckoutOut)
def ensure_checkout(appointment_id: UUID, db: Session = Depends(get_db)):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return crud.ensure_checkout(db, appointment)


@router.patch("/{appointment_id}/checkout", response_model=schemas.AppointmentCheckoutOut)
def update_checkout(
    appointment_id: UUID,
    payload: schemas.AppointmentCheckoutUpdate,
    db: Session = Depends(get_db),
):
    appointment = crud.get_appointment(db, appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    checkout = crud.ensure_checkout(db, appointment)
    return crud.update_checkout(db, checkout, payload)
