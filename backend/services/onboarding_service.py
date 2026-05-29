# Backend/services/onboarding_service.py
from db.redis_mongo import (
    get_user,
    upsert_user_if_not_exists,
    update_user_activity,
    get_session,
    create_session,
    mark_user_seen_in_redis
)
from utils.welcome_msg import generate_welcome
from services.whatsapp_service import send_text, send_menu_buttons
import time

def handle_new_user(phone):
    user = upsert_user_if_not_exists(phone)
    mark_user_seen_in_redis(phone)
    create_session(phone)

    welcome = generate_welcome(user.get("name"))
    send_text(phone, welcome)
    time.sleep(1)
    send_menu_buttons(phone)

    return {"type": "new_user", "welcome": welcome}


def handle_returning_user(phone, user):
    update_user_activity(phone)
    create_session(phone)

    welcome_back = generate_welcome(user.get("name"))
    send_text(phone, welcome_back)
    time.sleep(1)
    send_menu_buttons(phone)

    return {"type": "returning_user", "welcome": welcome_back}
