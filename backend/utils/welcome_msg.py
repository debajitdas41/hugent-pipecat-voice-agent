# backend/welcome_msg.py
import google.generativeai as genai
import os
from dotenv import load_dotenv

# Load .env from parent folder
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

# Configure Gemini API once
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def generate_welcome(is_returning=False, name=None):
    """
    Generates a dynamic welcome or welcome-back message.
    Works for all 6 scenarios.
    """

    # ----- BUILD PROMPT -----
    if not is_returning:
        # NEW USER FLOWS
        prompt = """
        You are SmileBot, a friendly WhatsApp AI assistant for a dental clinic.
        Create a short, friendly welcome message (2–3 lines):
        - Greet warmly
        - Mention clinic *SmileBright Dental Care*
        - Say you help with appointments, services & questions
        - Add 1–2 emojis max
        """
    else:
        # RETURNING USER FLOWS
        if name:
            prompt = f"""
            Generate a short welcome-back message for WhatsApp.
            
            Keep it friendly and human-like.
            Mention that you're ready to assist again.
            Keep it 1–2 lines with max 2 emojis.
            """
        else:
            prompt = """
            Generate a short welcome-back message for WhatsApp.
            Do NOT greet with name because we don't know it.
            Keep it friendly, 1–2 lines, max 2 emojis.
            """

    # ----- RUN MODEL -----
    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)

        if hasattr(response, "text"):
            return response.text.strip()

        return str(response).strip()

    except Exception as e:
        print("Error generating welcome message:", e)

        # Fallback messages
        if not is_returning:
            return "Hi there! 👋 Welcome to SmileBright Dental Care! I'm SmileBot, here to help you with appointments & services. 😄"
        else:
            if name:
                return f"Welcome back {name}! 👋 How can I help you today? 😄"
            return "Welcome back! 👋 How can I help you today? 😄"
