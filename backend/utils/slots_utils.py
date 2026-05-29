# Backend/utils/slots_utils.py
from datetime import datetime, time, timedelta
from utils.slots_booking import get_booked_count
from utils.timezone_utils import now_in_clinic

WEEKDAY_MAP = {
    "Mon": 0, "Tue": 1, "Wed": 2,
    "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6
}

def generate_slots_for_date(date_str, clinic):
    settings = clinic.get("slot_settings", {})

    # Check if clinic works that day
    now = now_in_clinic(clinic).replace(tzinfo=None)
    date_obj = datetime.strptime(date_str, "%d %b").replace(year=now.year)
    day = date_obj.strftime("%a")

    print("DEBUG-WD → selected:", day, "working_days:", settings.get("working_days"))

    if settings.get("working_days") and day not in settings["working_days"]:
        return []  # NOT a working day → no slots

    start = settings.get("start")
    end = settings.get("end")

    duration = settings.get("slot_minutes") or settings.get("slot_duration_minutes")
    duration = int(duration) if duration else 30

    capacity = int(settings.get("capacity_per_slot", 1))

     # 🆕 Buffer settings
    buffer_after_slots = int(settings.get("buffer_after_slots", 0))
    buffer_minutes = int(settings.get("buffer_minutes", 0))

    clinic_id = str(clinic["_id"])

    # Build base slot list
    # start_dt = datetime.strptime(start, "%H:%M")
    # end_dt = datetime.strptime(end, "%H:%M")
    try:
        start_dt = datetime.strptime(start, "%H:%M")
    except:
        start_dt = datetime.strptime(start, "%I:%M %p")  # AM/PM

    try:
        end_dt = datetime.strptime(end, "%H:%M")
    except:
        end_dt = datetime.strptime(end, "%I:%M %p")

    # print("DEBUG → start:", start, "end:", end, "parsed weekday:", day)

    slots = []
    slots_since_buffer = 0
    current_time = start_dt
    while current_time < end_dt:
        time_label = current_time.strftime("%I:%M %p")  # e.g., 10:30 AM

        booked = get_booked_count(clinic_id, date_str, time_label)
        available = capacity - booked
        is_full = available <= 0

        slots.append({
            "time": time_label,
            "capacity": capacity,
            "booked": booked,
            "available": available,
            "is_full": is_full
        })

        current_time += timedelta(minutes=duration)
        slots_since_buffer += 1
        # ⏸ Apply buffer if required
        if buffer_after_slots > 0 and slots_since_buffer >= buffer_after_slots:
            current_time += timedelta(minutes=buffer_minutes)
            slots_since_buffer = 0
        # else:
        #     current_time += timedelta(minutes=duration)

    # ⏳✨ FILTER OUT PAST TIMES ONLY IF SELECTED DATE IS TODAY
    try:
        today_str = now.strftime("%d %b").lstrip("0")  # "4 Dec"
        if date_str == today_str:  # selected date is today
            slots = [
                s for s in slots
                if datetime.strptime(s["time"], "%I:%M %p").replace(
                    year=now.year, month=now.month, day=now.day
                ) > now
            ]
    except:
        pass

    return slots
