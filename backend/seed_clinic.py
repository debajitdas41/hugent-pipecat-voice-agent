"""
seed_clinic.py — Creates a test clinic for voice agent testing.

Run this AFTER applying migrations:
    docker exec clinic-backend-api alembic upgrade head
    docker exec clinic-backend-api python seed_clinic.py

What it creates:
  - Clinic ID  : clinic_001
  - Name       : SmileBright Dental Care
  - Departments: Dental, Skin, Ortho, ENT
  - Slots      : 00:00 – 23:45, every 30 min, 2 patients per slot (matches real DB)
"""
import os
import sys
from datetime import datetime

# DATABASE_URL is injected by docker-compose
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://hugent:hugent@localhost:5432/hugentai",
)

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from db.models import Clinic
except ImportError as e:
    print(f"Import error: {e}")
    print("Run this script inside the container: docker exec clinic-backend-api python seed_clinic.py")
    sys.exit(1)

CLINIC_DATA = {
    "id": "clinic_001",
    "name": "SmileBright Dental Care",
    "owner_name": "Dr. Rahul Sharma",
    "phone_number": "+91-98302067486",
    "whatsapp_phone_number_id": "901118879759626",
    "clinic_type": "Multi",
    "departments": ["Dental", "Skin", "Ortho", "ENT"],
    "services": [],
    "slot_settings": {
        "start": "00:00",
        "end": "23:45",
        "slot_minutes": 30,
        "capacity_per_slot": 2,
        "working_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "buffer_minutes": 15,
        "closing_buffer": 30,
        "early_fill_min": 15,
        "late_grace_min": 7,
        "buffer_capacity": 1,
        "early_window_min": 45,
        "wait_weight": 0.2,
        "buffer_after_slots": 2,
    },
    "is_active": True,
    "created_at": datetime(2025, 11, 29, 0, 0, 0),
}


def seed():
    engine = create_engine(DATABASE_URL)
    with Session(engine) as db:
        existing = db.get(Clinic, CLINIC_DATA["id"])
        if existing:
            print(f"✓ Clinic '{CLINIC_DATA['id']}' already exists — skipping.")
            return

        clinic = Clinic(**CLINIC_DATA)
        db.add(clinic)
        db.commit()
        print(f"✓ Created clinic : {CLINIC_DATA['id']} — {CLINIC_DATA['name']}")
        print(f"  Departments    : {', '.join(CLINIC_DATA['departments'])}")
        ss = CLINIC_DATA["slot_settings"]
        print(f"  Slots          : {ss['start']} – {ss['end']}, "
              f"every {ss['slot_minutes']} min, "
              f"{ss['capacity_per_slot']} patients/slot")
        print()
        print("Set in voice agent .env:")
        print(f"  DEFAULT_CLINIC_ID=clinic_001")


if __name__ == "__main__":
    seed()
