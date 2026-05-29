from fastapi import APIRouter, Depends, HTTPException, status
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..db import get_db

router = APIRouter(prefix="/api/clinics", tags=["clinics"])


@router.post("", response_model=schemas.ClinicOut, status_code=status.HTTP_201_CREATED)
def create_clinic(payload: schemas.ClinicCreate, db: Session = Depends(get_db)):
    return crud.create_clinic(db, payload)


@router.get("", response_model=list[schemas.ClinicOut])
def list_clinics(db: Session = Depends(get_db)):
    return crud.list_clinics(db)


@router.get("/by-phone-number-id", response_model=schemas.ClinicOut)
def get_clinic_by_phone_number_id(phone_number_id: str, db: Session = Depends(get_db)):
    clinic = crud.get_clinic_by_phone_number_id(db, phone_number_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


@router.get("/{clinic_id}", response_model=schemas.ClinicOut)
def get_clinic(clinic_id: str, db: Session = Depends(get_db)):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return clinic


@router.patch("/{clinic_id}", response_model=schemas.ClinicOut)
def update_clinic(
    clinic_id: str,
    payload: schemas.ClinicUpdate,
    db: Session = Depends(get_db),
):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return crud.update_clinic(db, clinic, payload)


@router.patch("/{clinic_id}/timezone", response_model=schemas.ClinicOut)
def update_clinic_timezone(
    clinic_id: str,
    payload: schemas.ClinicTimezoneUpdate,
    db: Session = Depends(get_db),
):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")

    try:
        ZoneInfo(payload.timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    slot_settings = dict(clinic.slot_settings or {})
    slot_settings["timezone"] = payload.timezone

    return crud.update_clinic(db, clinic, schemas.ClinicUpdate(slot_settings=slot_settings))


@router.get("/{clinic_id}/whatsapp-credentials", response_model=schemas.ClinicWhatsAppCredentialOut)
def get_whatsapp_credentials(clinic_id: str, db: Session = Depends(get_db)):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    if not clinic.whatsapp_credentials:
        raise HTTPException(status_code=404, detail="WhatsApp credentials not found")
    return clinic.whatsapp_credentials


@router.put(
    "/{clinic_id}/whatsapp-credentials",
    response_model=schemas.ClinicWhatsAppCredentialOut,
)
def upsert_whatsapp_credentials(
    clinic_id: str,
    payload: schemas.ClinicWhatsAppCredentialIn,
    db: Session = Depends(get_db),
):
    clinic = crud.get_clinic(db, clinic_id)
    if not clinic:
        raise HTTPException(status_code=404, detail="Clinic not found")
    return crud.upsert_whatsapp_credentials(db, clinic, payload)
