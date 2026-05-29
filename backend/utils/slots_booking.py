############ count bookings for a slot ############
# Backend/utils/slots_utils.py

from db.redis_mongo import appointments_col

def get_booked_count(clinic_id, date_str, time_str):
    """Return how many bookings exist for this clinic/date/time."""
    return appointments_col.count_documents({
        "clinic_id": clinic_id,
        "date": date_str,
        "time": time_str
    })
