# Backend/services/appointment_flow.py
from db.redis_mongo import get_session, update_session, get_user, users_col
from services.whatsapp_service import send_text
from datetime import datetime


# -----------------------
# ENTRY POINT
# -----------------------
def start_appointment_flow(phone):
    """
    User clicked on 'Book Apt' – Start the flow
    """
    print("➡ Starting appointment flow")

    session = {
        "current_flow": "appointment",
        "step": "ask_name",
        "temp_name": "",
        "temp_reason": "",
        "temp_date": "",
        "temp_time": ""
    }

    update_session(phone, session)

    user = get_user(phone)

    # If name already stored, skip asking name
    if user and user.get("name"):
        update_session(phone, {"step": "ask_reason", "temp_name": user.get("name")})
        return send_text(phone, f"Great {user['name']}! 😊\nWhat is the reason for your appointment?")

    return send_text(phone, "Sure! Let's book your appointment. 😊\nFirst, may I know your *name*?")


# -----------------------
# PROCESS USER REPLIES
# -----------------------
def handle_appointment_reply(phone, user_msg):
    """
    Handles step-by-step flow based on Redis 'session'
    """
    session = get_session(phone)
    if not session:
        return send_text(phone, "Your session expired. Please type 'Book Apt' again 🙂")

    step = session.get("step")

    # ---------------------------
    # STEP 1: Ask Name
    # ---------------------------
    if step == "ask_name":
        update_session(phone, {"temp_name": user_msg, "step": "ask_reason"})
        return send_text(phone, f"Thanks {user_msg}! 😊\nWhat is the appointment for? (e.g., Cleaning, Tooth Pain, Braces)")

    # ---------------------------
    # STEP 2: Ask Reason
    # ---------------------------
    if step == "ask_reason":
        update_session(phone, {"temp_reason": user_msg, "step": "ask_date"})
        return send_text(phone, "Got it 👍\nWhat *date* would you prefer? (Example: 23 Feb or 23/02/2025)")

    # ---------------------------
    # STEP 3: Ask Date
    # ---------------------------
    if step == "ask_date":
        update_session(phone, {"temp_date": user_msg, "step": "ask_time"})
        return send_text(phone, "Perfect! ⏰\nWhat *time* do you prefer? (Example: 4 PM or 16:00)")

    # ---------------------------
    # STEP 4: Ask Time
    # ---------------------------
    if step == "ask_time":
        update_session(phone, {"temp_time": user_msg, "step": "confirm"})

        # Build confirmation message
        name = session["temp_name"]
        reason = session["temp_reason"]
        date = session["temp_date"]
        time_ = user_msg

        confirmation_msg = (
            f"Almost done! 🎉\n\nPlease confirm your appointment:\n"
            f"👤 Name: *{name}*\n"
            f"🦷 Purpose: *{reason}*\n"
            f"📅 Date: *{date}*\n"
            f"⏰ Time: *{time_}*\n\n"
            "Reply *yes* to confirm or *no* to cancel."
        )
        return send_text(phone, confirmation_msg)

    # ---------------------------
    # STEP 5: Confirm
    # ---------------------------
    if step == "confirm":
        if user_msg.lower() in ["yes", "y", "confirm"]:
            # Save in MongoDB under "history"
            save_appointment_to_mongo(phone, session)
            update_session(phone, {"current_flow": "", "step": ""})

            return send_text(phone, "Your appointment is confirmed! 🎉\nOur team will contact you shortly.")

        else:
            update_session(phone, {"current_flow": "", "step": ""})
            return send_text(phone, "Okay, appointment cancelled. Let me know if you need anything else 😊")

    return send_text(phone, "Sorry, I didn’t understand that. Please try again 🙂")


# ---------------------------
# SAVE TO MONGODB
# ---------------------------
def save_appointment_to_mongo(phone, session):
    print("Saving appointment to MongoDB")

    appointment = {
        "name": session["temp_name"],
        "reason": session["temp_reason"],
        "date": session["temp_date"],
        "time": session["temp_time"],
        "timestamp": datetime.now()
    }

    users_col.update_one(
        {"phone": phone},
        {"$push": {"history": appointment}}
    )
