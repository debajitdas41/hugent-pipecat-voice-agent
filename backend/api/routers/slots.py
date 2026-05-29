from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import crud
from ..db import get_db
from utils.slots_utils import generate_slots_for_date

router = APIRouter(prefix="/api/slots", tags=["slots"])


@router.get("/availability")
def slot_availability(
    clinic_id: str,
    date: str,
    time: str,
    db: Session = Depends(get_db),
):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    capacity = int((clinic.slot_settings or {}).get("capacity_per_slot", 1))
    booked = crud.count_appointments_for_slot(db, clinic_id, date, time)
    available = max(capacity - booked, 0)

    return {
        "clinic_id": clinic_id,
        "date": date,
        "time": time,
        "capacity": capacity,
        "booked": booked,
        "available": available,
        "is_full": booked >= capacity,
    }


@router.get("/available")
def available_slots(
    clinic_id: str,
    date: str,
    db: Session = Depends(get_db),
):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    clinic_dict = {
        "_id": clinic.id,
        "slot_settings": clinic.slot_settings or {},
    }
    slots = generate_slots_for_date(date, clinic_dict)
    available_times = [slot["time"] for slot in slots if not slot.get("is_full")]

    return {
        "clinic_id": clinic_id,
        "date": date,
        "available_times": available_times,
    }
