# Backend/utils/date_utils.py
from datetime import datetime, timedelta

from utils.timezone_utils import now_in_clinic

def get_next_7_dates(clinic=None):
    today = now_in_clinic(clinic).replace(tzinfo=None)
    dates = []
    for i in range(7):
        d = today + timedelta(days=i)
        day = d.strftime("%d").lstrip("0")
        formatted = f"{day} {d.strftime('%b')}"   # 3 Dec, 4 Dec etc.

        if i == 0:
            prefix = "Today"
        elif i == 1:
            prefix = "Tomorrow"
        else:
            prefix = d.strftime("%a")  # Mon, Tue, Wed...

        label = f"{prefix} • {formatted}"
        dates.append({"label": label, "value": formatted})
    return dates


def normalize_date_text(raw_text: str) -> str | None:
    """
    Convert user text date -> D Mon (3 Dec, 14 Jan etc.)
    For now supports only exact D Mon like '5 Dec'
    GPT support will be plugged later.
    """
    if not raw_text:
        return None

    raw = raw_text.strip()
    try:
        d = datetime.strptime(raw, "%d %b")
        day = d.strftime("%d").lstrip("0")
        return f"{day} {d.strftime('%b')}"
    except:
        return None
