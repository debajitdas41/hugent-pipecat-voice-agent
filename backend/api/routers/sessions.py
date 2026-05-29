from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import redis_mongo

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    phone: str
    clinic_id: str
    ttl: int | None = None
    data: dict | None = None


class SessionUpdate(BaseModel):
    data: dict
    ttl: int | None = None


@router.get("")
def get_session(phone: str, clinic_id: str):
    session = redis_mongo.get_session(phone, clinic_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/create")
def create_session(payload: SessionCreate):
    redis_mongo.create_session(payload.phone, payload.clinic_id, ttl=payload.ttl)
    if payload.data:
        redis_mongo.update_session(payload.phone, payload.clinic_id, payload.data, ttl=payload.ttl)
    session = redis_mongo.get_session(payload.phone, payload.clinic_id) or {}
    return session


@router.patch("")
def update_session(phone: str, clinic_id: str, payload: SessionUpdate):
    redis_mongo.update_session(phone, clinic_id, payload.data, ttl=payload.ttl)
    session = redis_mongo.get_session(phone, clinic_id) or {}
    return session
