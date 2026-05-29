from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import crud, schemas
from ..db import get_db

router = APIRouter(prefix="/api/user-clinics", tags=["user-clinics"])


@router.post("", response_model=schemas.UserClinicOut, status_code=status.HTTP_201_CREATED)
def create_user_clinic(payload: schemas.UserClinicCreate, db: Session = Depends(get_db)):
    return crud.create_user_clinic(db, payload)


@router.get("", response_model=schemas.UserClinicOut)
def get_user_clinic(user_id: UUID, clinic_id: str, db: Session = Depends(get_db)):
    link = crud.get_user_clinic(db, user_id, clinic_id)
    if not link:
        raise HTTPException(status_code=404, detail="User clinic link not found")
    return link


@router.patch("/{link_id}", response_model=schemas.UserClinicOut)
def update_user_clinic(
    link_id: int,
    payload: schemas.UserClinicUpdate,
    db: Session = Depends(get_db),
):
    link = crud.get_user_clinic_by_id(db, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="User clinic link not found")
    return crud.update_user_clinic(db, link, payload)


@router.post("/{link_id}/touch", response_model=schemas.UserClinicOut)
def touch_user_clinic(link_id: int, db: Session = Depends(get_db)):
    link = crud.get_user_clinic_by_id(db, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="User clinic link not found")
    return crud.touch_user_clinic(db, link)


@router.post("/touch", response_model=schemas.UserClinicOut)
def touch_user_clinic_by_pair(user_id: UUID, clinic_id: str, db: Session = Depends(get_db)):
    link = crud.get_user_clinic(db, user_id, clinic_id)
    if not link:
        raise HTTPException(status_code=404, detail="User clinic link not found")
    return crud.touch_user_clinic(db, link)


@router.post("/{link_id}/history", response_model=schemas.UserClinicOut)
def append_user_clinic_history(
    link_id: int,
    payload: schemas.UserClinicHistoryAppend,
    db: Session = Depends(get_db),
):
    link = crud.get_user_clinic_by_id(db, link_id)
    if not link:
        raise HTTPException(status_code=404, detail="User clinic link not found")
    return crud.append_user_clinic_history(db, link, payload.item)
