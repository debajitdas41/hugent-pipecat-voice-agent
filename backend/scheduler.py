import requests
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from db.redis_mongo import clinics_col
import os

API_URL = os.getenv("SLOT_TICK_API_URL", "http://backend-api:8000/slot_tick")
NOTIFY_URL = os.getenv("NOTIFY_API_URL", "http://backend-api:8000/notify_appointments")
NOTIFY_DELAY_URL = os.getenv("NOTIFY_DELAY_URL", "http://backend-api:8000/notify_delays")

def is_clinic_open(clinic):
    now = datetime.now().time()
    start = datetime.strptime(
        clinic["slot_settings"]["start"], "%H:%M"
    ).time()
    end = datetime.strptime(
        clinic["slot_settings"]["end"], "%H:%M"
    ).time()

    return start <= now <= end

def slot_tick_job():
    try:
        clinics = clinics_col.find({}, {"_id": 1})

        for clinic in clinics:
            clinic_id = str(clinic["_id"])

            try:
                requests.post(
                    API_URL,
                    json={"clinic_id": clinic_id},
                    timeout=5
                )
                print(f"slot_tick executed for clinic {clinic_id}")
            except Exception as e:
                print(f"slot_tick error for clinic {clinic_id}:", e)

    except Exception as e:
        print("slot_tick global error:", e)


def notify_job():
    try:
        clinics = clinics_col.find({}, {"_id": 1})

        for clinic in clinics:
            clinic_id = str(clinic["_id"])
            try:
                requests.post(
                    NOTIFY_URL,
                    json={"clinic_id": clinic_id, "window_minutes": 15},
                    timeout=5
                )
                print(f"notify executed for clinic {clinic_id}")
            except Exception as e:
                print(f"notify error for clinic {clinic_id}:", e)

    except Exception as e:
        print("notify global error:", e)


def notify_delay_job():
    try:
        clinics = clinics_col.find({}, {"_id": 1})

        for clinic in clinics:
            clinic_id = str(clinic["_id"])
            try:
                requests.post(
                    NOTIFY_DELAY_URL,
                    json={"clinic_id": clinic_id},
                    timeout=5
                )
                print(f"notify_delay executed for clinic {clinic_id}")
            except Exception as e:
                print(f"notify_delay error for clinic {clinic_id}:", e)

    except Exception as e:
        print("notify_delay global error:", e)

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(slot_tick_job, "interval", minutes=1)
    scheduler.add_job(notify_job, "interval", minutes=1)
    scheduler.add_job(notify_delay_job, "interval", minutes=5)
    scheduler.start()


if __name__ == "__main__":
    start_scheduler()
    try:
        while True:
            pass
    except KeyboardInterrupt:
        pass
