"""
Booking Processor — Non-blocking business logic for voice booking pipeline.

Key architecture decisions (v2 → pipecat latency fix):
- process_frame NEVER awaits business logic — spawns asyncio.create_task
- _patch_context() updates LLMContext.messages[0] in-place (0ms, no API calls)
- TranscriptionFrame pushed IMMEDIATELY after patch — LLM starts at once
- Extraction + process_turn run in BACKGROUND after push_frame (parallel with LLM)
- _last_booking_context: cached from previous turn, injected into current turn prompt
  (1 turn behind, but saves 2.8s of blocking per turn)
- clinic_info pre-fetched at session start (passed via constructor)
"""
import asyncio
import base64
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TextFrame,
    UserStartedSpeakingFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.services.settings import TTSSettings
from pipecat.transcriptions.language import Language
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from sarvamai import AsyncSarvamAI, AudioOutput, EventResponse

from booking.manager import VoiceBookingManager
from services.extraction import extract_booking_info
from prompts.loader import get_system_prompt, get_smart_prompt

# Sarvam TTS language codes (same as v2's SUPPORTED_LANGUAGES)
_SARVAM_LANG_MAP: dict[str, str] = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "gu": "gu-IN",
    "kn": "kn-IN", "ml": "ml-IN", "mr": "mr-IN", "od": "od-IN",
    "pa": "pa-IN", "ta": "ta-IN", "te": "te-IN",
}

# ── Filler bypass: these utterances skip the LLM entirely ──────────────────
_PURE_FILLER_RE = re.compile(
    r"""^[\s\W]*(
        hmm+|hm+|uh+|um+|ah+|oh+
        |okay|ok|yeah|yep|yup
        |haan|ha+n?|ji
        |\u0939\u093e\u0901|\u0939\u093e\u0902|\u0939\u092e\u094d\u092e|\u0920\u0940\u0915|\u0905\u091a\u094d\u091b\u093e
        |\u09b9\u09cd\u09af\u09be\u0981|\u09b9\u09c1\u09ae
        |\u0b9a\u0bb0\u0bbf|\u0bb9\u0bc2\u0bae\u0bcd
        |\u0c05\u0c35\u0c41\u0c28\u0c41|\u0c39\u0c2e\u0c4d\u0c2e\u0c4d
        |\u0c39\u0ccc\u0ca6\u0cc1|\u0cb9\u0ccd\u0cae\u0ccd
        |\u0d36\u0d30\u0d3f|\u0d39\u0d4d\u200c\u0d2e\u0d4d
        |\u0939\u094b|\u0920\u0940\u0915
        |\u0ab9\u0abe|\u0ab9\u0acd\u0aae
        |\u0a39\u0abe\u0a02|\u0a39\u0a71\u0aae
        |\u0b39\u0b01|\u0b39\u0b4d\u0bae
    )[\s\W,\.!?]*$""",
    re.IGNORECASE | re.UNICODE | re.VERBOSE,
)

_FILLER_BRIDGE: dict[str, str] = {
    "en": "Sure, go ahead.",
    "hi": "\u0939\u093e\u0901, \u092c\u0924\u093e\u0907\u090f\u0964",
    "bn": "\u09b9\u09cd\u09af\u09be\u0981, \u09ac\u09b2\u09c1\u09a8\u0964",
    "ta": "\u0b9a\u0bb0\u0bbf, \u0b9a\u0bca\u0bb2\u0bcd\u0bb2\u0bc1\u0b99\u0bcd\u0b95\u0bb3\u0bcd.",
    "te": "\u0c05\u0c35\u0c41\u0c28\u0c41, \u0c1a\u0c46\u0c2a\u0c4d\u0c2a\u0c02\u0c21\u0c3f.",
    "kn": "\u0cb9\u0ccc\u0ca6\u0cc1, \u0cb9\u0cc7\u0cb3\u0cbf.",
    "ml": "\u0d36\u0d30\u0d3f, \u0d2a\u0d31\u0d1e\u0d4d\u0d1e\u0d4b\u0d33\u0d42.",
    "mr": "\u0939\u094b, \u0938\u093e\u0902\u0917\u093e.",
    "gu": "\u0ab9\u0abe, \u0a95\u0ab9\u0acb.",
    "pa": "\u0a39\u0abe\u0a02, \u0a26\u0a71\u0a38\u0acb.",
    "od": "\u0b39\u0b01, \u0b15\u0b41\u0b39\u0b28\u0b4d\u0b24\u0b41\u0964",
}

# ── Language code → Pipecat Language enum (for TTSUpdateSettingsFrame) ────────
_LANG_TO_PIPECAT: dict[str, Language] = {
    "en": Language.EN_IN,
    "hi": Language.HI_IN,
    "bn": Language.BN_IN,
    "gu": Language.GU_IN,
    "kn": Language.KN_IN,
    "ml": Language.ML_IN,
    "mr": Language.MR_IN,
    "od": Language.OR_IN,
    "pa": Language.PA_IN,
    "ta": Language.TA_IN,
    "te": Language.TE_IN,
}

# ── Greeting helper ─────────────────────────────────────────────────────────
def _time_greeting() -> str:
    """Return Good morning / afternoon / evening based on IST time."""
    tz_name = os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
    try:
        hour = datetime.now(ZoneInfo(tz_name)).hour
    except Exception:
        hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    else:
        return "Good evening"

_LANGUAGE_KEYWORDS: dict[str, str] = {
    # English names
    "english": "en", "hindi": "hi", "bengali": "bn", "bangla": "bn",
    "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "marathi": "mr",
    "odia": "od", "oriya": "od", "punjabi": "pa", "tamil": "ta", "telugu": "te",
    # Phonetic / STT variants
    "hindhi": "hi", "hind": "hi", "hindee": "hi",
    "banglay": "bn", "bangali": "bn", "bengla": "bn",
    "kannad": "kn", "kannda": "kn",
    "tamiz": "ta", "tamizh": "ta",
    "telgu": "te", "telegu": "te",
    "malay": "ml", "malaylam": "ml",
    "marath": "mr",
    "gujara": "gu",
    "panjabi": "pa", "panjab": "pa",
    # Native scripts
    "\u0939\u093f\u0902\u0926\u0940": "hi", "\u0939\u093f\u0928\u094d\u0926\u0940": "hi",
    "\u09ac\u09be\u0982\u09b2\u09be": "bn", "\u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0": "gu",
    "\u0c95\u0ca8\u0ccd\u0ccd\u0ca1": "kn", "\u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d02": "ml",
    "\u092e\u0930\u093e\u0920\u0940": "mr", "\u0b13\u0b21\u093c\u093f\u0906": "od",
    "\u0a2a\u0a70\u0a1c\u0a3e\u0a2c\u0a40": "pa", "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd": "ta",
    "\u0c24\u0c46\u0c32\u0c41\u0c17\u0c41": "te",
    # Language names spoken in Hindi/Devanagari
    "\u092c\u0902\u0917\u093e\u0932\u0940": "bn", "\u092c\u0902\u0917\u0932\u093e": "bn",
    "\u092c\u093e\u0902\u0917\u094d\u0932\u093e": "bn",
    "\u0924\u092e\u093f\u0932": "ta", "\u0924\u093e\u092e\u093f\u0932": "ta",
    "\u0915\u0928\u094d\u0928\u0921": "kn", "\u0915\u0928\u094d\u0928\u0921\u093c": "kn",
    "\u0924\u0947\u0932\u0941\u0917\u0941": "te", "\u0924\u0947\u0932\u0941\u0917\u0942": "te",
    "\u092e\u0932\u092f\u093e\u0932\u092e": "ml", "\u092e\u0932\u092f\u093e\u0933\u092e": "ml",
    "\u0917\u0941\u091c\u0930\u093e\u0924\u0940": "gu",
    "\u092a\u0902\u091c\u093e\u092c\u0940": "pa", "\u092a\u091e\u094d\u091c\u093e\u092c\u0940": "pa",
    "\u0909\u0921\u093c\u093f\u092f\u093e": "od", "\u0913\u0921\u093f\u092f\u093e": "od",
    "\u0909\u0921\u093f\u092f\u093e": "od",
}

_LANGUAGE_ACK: dict[str, str] = {
    "en": "Great! Let's continue in English. I'll help you book an appointment. Please tell me your name.",
    "hi": "\u092c\u0939\u0941\u0924 \u0905\u091a\u094d\u091b\u093e! \u0905\u092c \u0939\u092e \u0939\u093f\u0902\u0926\u0940 \u092e\u0947\u0902 \u092c\u093e\u0924 \u0915\u0930\u0947\u0902\u0917\u0947\u0964 \u092e\u0948\u0902 \u0906\u092a\u0915\u093e appointment book \u0915\u0930\u0942\u0901\u0917\u0940\u0964 \u0915\u0943\u092a\u092f\u093e \u0905\u092a\u0928\u093e \u0928\u093e\u092e \u092c\u0924\u093e\u0907\u090f\u0964",
    "bn": "\u09a6\u09be\u09b0\u09c1\u09a3! \u098f\u0996\u09a8 \u0986\u09ae\u09b0\u09be \u09ac\u09be\u0982\u09b2\u09be\u09af\u09bc \u0995\u09a5\u09be \u09ac\u09b2\u09ac\u0964 \u0986\u09ae\u09bf \u0986\u09aa\u09a8\u09be\u09b0 appointment book \u0995\u09b0\u09ac\u0964 \u0986\u09aa\u09a8\u09be\u09b0 \u09a8\u09be\u09ae \u09ac\u09b2\u09c1\u09a8 please\u0964",
    "gu": "Sure! \u0ab9\u0bb5\u0ac7 \u0a86\u0aaa\u0aa3\u0ac7 \u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0\u0aae\u0abe\u0a82 \u0ab5\u0abe\u0aa4 \u0a95\u0ab0\u0ac0\u0ab6\u0ac1\u0a82. \u0ab9\u0ac1\u0a82 \u0aa4\u0aae\u0abe\u0ab0\u0ac1\u0a82 appointment book \u0a95\u0ab0\u0ac0\u0ab6. \u0aa4\u0aae\u0abe\u0ab0\u0ac1\u0a82 \u0aa8\u0abe\u0aae \u0a95\u0ab9\u0acb please.",
    "kn": "Super! \u0c08\u0c17 \u0ca8\u0cbe\u0cb5\u0cc1 \u0c95\u0ca8\u0ccd\u0ccd\u0ca1\u0ca6\u0cb2\u0ccd\u0cb2\u0cbf \u0cae\u0cbe\u0ca4\u0ca8\u0cbe\u0ca1\u0ccb\u0ca3. \u0ca8\u0cbe\u0ca8\u0cc1 \u0ca8\u0cbf\u0cae\u0ccd\u0cae appointment book \u0cae\u0cbe\u0ca1\u0cc1\u0ca4\u0ccd\u0ca4\u0cc7\u0ca8\u0cc6. \u0ca6\u0caf\u0cb5\u0cbf\u0c9f\u0ccd\u0c9f\u0cc1 \u0ca8\u0cbf\u0cae\u0ccd\u0cae \u0cb9\u0cc6\u0cb8\u0cb0\u0cc1 \u0cb9\u0cc7\u0cb3\u0cbf.",
    "ml": "Super! \u0d07\u0d28\u0d3f \u0d28\u0d2e\u0d4d\u0d2e\u0d33\u0d4d\u200d \u0d2e\u0d32\u0d2f\u0d3e\u0d33\u0d24\u0d4d\u0d24\u0d3f\u0d32\u0d4d\u200d \u0d38\u0d02\u0d38\u0d3e\u0d30\u0d3f\u0d15\u0d4d\u0d15\u0d3e\u0d02. \u0d1e\u0d3e\u0d7f \u0d28\u0d3f\u0d19\u0d4d\u0d19\u0d33\u0d41\u0d1f\u0d46 appointment book \u0d1a\u0d46\u0d2f\u0d4d\u0d2f\u0d3e\u0d02. \u0d2a\u0d47\u0d30\u0d4d\u200d \u0d2a\u0d31\u0d2f\u0d3e\u0d2e\u0d4b?",
    "mr": "Sure! \u0906\u0924\u093e \u0906\u092a\u0923 \u092e\u0930\u093e\u0920\u0940\u0924 \u092c\u094b\u0932\u0942\u092f\u093e. \u092e\u0940 \u0924\u0941\u092e\u091a\u0902 appointment book \u0915\u0930\u0947\u0928. \u0915\u0943\u092a\u092f\u093e \u0924\u0941\u092e\u091a\u0902 \u0928\u093e\u0935 \u0938\u093e\u0902\u0917\u093e.",
    "od": "Sure! \u0b0f\u0b2c\u0b47 \u0b06\u0b2e\u0b47 \u0b13\u0b21\u093c\u093f\u0906\u0b30\u0b47 \u0b15\u0b25\u0b3e \u0b39\u0b47\u0b2c\u0b3e\u0964 \u0b2e\u0b41\u0b01 \u0b06\u0b2a\u0b23\u0b19\u0b4d\u0b15 appointment book \u0b15\u0b30\u0b3f\u0b2c\u0b3f\u0964 \u0b26\u0b2f\u0b3e\u0b15\u0b30\u0b3f \u0b28\u0b3e\u0b2e \u0b15\u0b41\u0b39\u0b28\u0b4d\u0b24\u0b41\u0964",
    "pa": "Bahut vadiya! \u0a39\u0a41\u0a23 \u0a05\u0a38\u0a40\u0a02 \u0a2a\u0a70\u0a1c\u0a3e\u0a2c\u0a40 \u0a35\u0a3f\u0a71\u0a1a \u0a17\u0a71\u0a32 \u0a15\u0a30\u0a3e\u0a02\u0a17\u0a47\u0964 \u0a2e\u0a48\u0a02 \u0a24\u0a41\u0a39\u0a3e\u0a21\u0a40 appointment book \u0a15\u0a30\u0a3e\u0a02\u0a17\u0a40\u0964 \u0a15\u0a3f\u0a30\u0a2a\u0a3e \u0a15\u0a30\u0a15\u0a47 \u0a06\u0a2a\u0a23\u0a3e \u0a28\u0a3e\u0a2e \u0a26\u0a71\u0a38\u0acb\u0964",
    "ta": "Super! \u0b87\u0ba9\u0bbf \u0ba8\u0bbe\u0bae \u0ba4\u0bae\u0bbf\u0bb4\u0bbf\u0bb2\u0bcd \u0baa\u0bc7\u0b9a\u0bc1\u0bb5\u0bcb\u0bae\u0bcd. \u0ba8\u0bbe\u0ba9\u0bcd \u0b89\u0b99\u0bcd\u0b95\u0bb3\u0bc1\u0b95\u0bcd\u0b95\u0bc1 appointment book \u0baa\u0ba3\u0bcd\u0ba3\u0bc1\u0bb1\u0bc7\u0ba9\u0bcd. \u0b89\u0b99\u0bcd\u0b95 \u0baa\u0bc7\u0bb0\u0bc1 \u0b9a\u0bca\u0bb2\u0bcd\u0bb2\u0bc1\u0b99\u0bcd\u0b95.",
    "te": "Super! \u0c07\u0c2a\u0c4d\u0c2a\u0c41\u0c21\u0c41 \u0c2e\u0c28\u0c02 \u0c24\u0c46\u0c32\u0c41\u0c17\u0c41\u0c32\u0c4b \u0c2e\u0c3e\u0c1f\u0c4d\u0c32\u0c3e\u0c21\u0c41\u0c15\u0c41\u0c02\u0c26\u0c3e\u0c02. \u0c28\u0c47\u0c28\u0c41 \u0c2e\u0c40 appointment book \u0c1a\u0c47\u0c38\u0c4d\u0c24\u0c3e\u0c28\u0c41. \u0c26\u0c2f\u0c1a\u0c47\u0c38\u0c3f \u0c2e\u0c40 \u0c2a\u0c47\u0c30\u0c41 \u0c1a\u0c46\u0c2a\u0c4d\u0c2a\u0c02\u0c21\u0c3f.",
}

_LANGUAGE_REPROMPT: dict[str, str] = {
    "en": "Sorry, I didn't catch that. Which language would you prefer — English, Hindi, Tamil, Bengali, or any other?",
    "hi": "\u0915\u094d\u0937\u092e\u093e \u0915\u0930\u0947\u0902, \u0915\u094c\u0928 \u0938\u0940 \u092d\u093e\u0937\u093e \u092e\u0947\u0902 \u092c\u093e\u0924 \u0915\u0930\u0928\u093e \u091a\u093e\u0939\u0947\u0902\u0917\u0947? \u0939\u093f\u0902\u0926\u0940, English, Tamil, Bengali, \u092f\u093e \u0915\u094b\u0908 \u0914\u0930?",
    "bn": "\u09a6\u09c1\u0983\u0996\u09bf\u09a4, \u0986\u09aa\u09a8\u09bf \u0995\u09cb\u09a8 \u09ad\u09be\u09b7\u09be\u09af\u09bc \u0995\u09a5\u09be \u09ac\u09b2\u09a4\u09c7 \u099a\u09be\u09a8? \u09ac\u09be\u0982\u09b2\u09be, English, Hindi, Tamil, \u09a8\u09be\u0995\u09bf \u0985\u09a8\u09cd\u09af \u0995\u09cb\u09a8\u09cb?",
    "pa": "\u0a2e\u0a3e\u0a2b\u0a3c \u0a15\u0a30\u0a28\u0a3e, \u0a24\u0a41\u0a38\u0a40\u0a02 \u0a15\u0a3f\u0a39\u0a5c\u0a40 \u0a2d\u0a3e\u0a38\u0a3c\u0a3e \u0a35\u0a3f\u0a71\u0a1a \u0a17\u0a71\u0a32 \u0a15\u0a30\u0a28\u0a3e \u0a1a\u0a3e\u0a39\u0a41\u0a70\u0a26\u0a47 \u0a39\u0a4b? \u0a2a\u0a70\u0a1c\u0a3e\u0a2c\u0a40, English, Hindi, \u0a1c\u0a3e\u0a02 \u0a15\u0a4b\u0a08 \u0a39\u0a4b\u0a30?",
    "gu": "\u0aae\u0abe\u0aab \u0a95\u0ab0\u0a9c\u0acb, \u0aa4\u0aae\u0ac7 \u0a95\u0a88 \u0aad\u0abe\u0ab7\u0abe\u0aae\u0abe\u0a82 \u0ab5\u0abe\u0aa4 \u0a95\u0ab0\u0ab5\u0abe \u0a87\u0a9a\u0acd\u0a9b\u0acb \u0a9b\u0acb? \u0a97\u0ac1\u0a9c\u0ab0\u0abe\u0aa4\u0ac0, English, Hindi, \u0a85\u0aa5\u0ab5\u0abe \u0aac\u0ac0\u0a9c\u0ac0 \u0a95\u0acb\u0a88?",
    "ta": "\u0bae\u0ba9\u0bcd\u0ba9\u0bbf\u0b95\u0bcd\u0b95\u0bb5\u0bc1\u0bae\u0bcd, \u0ba8\u0bc0\u0b99\u0bcd\u0b95\u0bb3\u0bcd \u0b8e\u0ba8\u0bcd\u0ba4 \u0bae\u0bca\u0bb4\u0bbf\u0baf\u0bbf\u0bb2\u0bcd \u0baa\u0bc7\u0b9a \u0bb5\u0bbf\u0bb0\u0bc1\u0bae\u0bcd\u0baa\u0bc1\u0b95\u0bbf\u0bb1\u0bc0\u0bb0\u0bcd\u0b95\u0bb3\u0bcd? \u0ba4\u0bae\u0bbf\u0bb4\u0bcd, English, Hindi, \u0b85\u0bb2\u0bcd\u0bb2\u0ba4\u0bc1 \u0bb5\u0bc7\u0bb1\u0bc1 \u0b8f\u0ba4\u0bbe\u0bb5\u0ba4\u0bc1?",
    "te": "\u0c15\u0c4d\u0c37\u0c2e\u0c3f\u0c02\u0c1a\u0c02\u0c21\u0c3f, \u0c2e\u0c40\u0c30\u0c41 \u0c0f \u0c2d\u0c3e\u0c37\u0c32\u0c4b \u0c2e\u0c3e\u0c1f\u0c4d\u0c32\u0c3e\u0c21\u0c3e\u0c32\u0c28\u0c41\u0c15\u0c41\u0c02\u0c1f\u0c41\u0c28\u0c4d\u0c28\u0c3e\u0c30\u0c41? \u0c24\u0c46\u0c32\u0c41\u0c17\u0c41, English, Hindi, \u0c32\u0c47\u0c26\u0c3e \u0c2e\u0c30\u0c4a\u0c15\u0c1f\u0c3e?",
    "kn": "\u0c95\u0ccd\u0cb7\u0cae\u0cbf\u0cb8\u0cbf, \u0ca8\u0cc0\u0cb5\u0cc1 \u0caf\u0cbe\u0cb5 \u0cad\u0cbe\u0cb7\u0cc6\u0caf\u0cb2\u0ccd\u0cb2\u0cbf \u0cae\u0cbe\u0ca4\u0cbe\u0ca1\u0cb2\u0cc1 \u0cac\u0caf\u0cb8\u0cc1\u0ca4\u0ccd\u0ca4\u0cc0\u0cb0\u0cbf? \u0c95\u0ca8\u0ccd\u0ccd\u0ca1, English, Hindi, \u0c85\u0ca5\u0cb5\u0cbe \u0cac\u0cc7\u0cb0\u0cc6 \u0caf\u0cbe\u0cb5\u0cc1\u0ca6\u0cbe\u0ca6\u0cb0\u0cc2?",
    "ml": "\u0d15\u0d4d\u0d37\u0d2e\u0d3f\u0d15\u0d4d\u0d15\u0d23\u0d02, \u0d0f\u0d24\u0d4d \u0d2d\u0d3e\u0d37\u0d2f\u0d3f\u0d32\u0d4d\u200d \u0d38\u0d02\u0d38\u0d3e\u0d30\u0d3f\u0d15\u0d4d\u0d15\u0d23\u0d02? Malayalam, English, Hindi, \u0d05\u0d24\u0d4b \u0d2e\u0d31\u0d4d\u0d31\u0d4d \u0d0f\u0d24\u0d46\u0d19\u0d4d\u0d15\u0d3f\u0d32\u0d41\u0d02?",
    "mr": "\u092e\u093e\u092b \u0915\u0930\u093e, \u0924\u0941\u092e\u094d\u0939\u0940 \u0915\u094b\u0923\u0924\u094d\u092f\u093e \u092d\u093e\u0937\u0947\u0924 \u092c\u094b\u0932\u0942 \u0907\u091a\u094d\u091b\u093f\u0924\u093e? \u092e\u0930\u093e\u0920\u0940, English, Hindi, \u0915\u093f\u0902\u0935\u093e \u0907\u0924\u0930 \u0915\u094b\u0923\u0924\u0940?",
    "od": "\u0b15\u0b4d\u0b37\u0b2e\u0b3e \u0b15\u0b30\u0b28\u0b4d\u0b24\u0b41, \u0b06\u0b2a\u0b23 \u0b15\u0b47\u0b09\u0b01 \u0b2d\u0b3e\u0b37\u0b3e\u0b30\u0b47 \u0b15\u0b25\u0b3e \u0b39\u0b47\u0b2c\u0b3e\u0b15\u0b41 \u0b1a\u0b3e\u0b39\u0b41\u0b01\u0b1b\u0b28\u0b4d\u0b24\u0b3f? \u0b13\u0b21\u093c\u093f\u0906, English, Hindi, \u0b2c\u0b3e \u0b05\u0b28\u0b4d\u0b2f \u0b15\u0b4c\u0b23\u0b38\u0b3f?",
}

# ── Language-detection helpers ──────────────────────────────────────────────

_LANG_NAMES: dict[str, str] = {
    "hi": "Hindi",  "bn": "Bengali", "gu": "Gujarati", "kn": "Kannada",
    "ml": "Malayalam", "mr": "Marathi", "od": "Odia",  "pa": "Punjabi",
    "ta": "Tamil",  "te": "Telugu",
}

# Maps STT-reported language code strings → our internal language codes.
# Covers both "hi-IN" (Pipecat Language enum .value) and bare "hi" forms.
_STT_LANG_MAP: dict[str, str] = {
    "en-in": "en", "en-us": "en", "en-gb": "en", "en": "en",
    "hi-in": "hi", "hi": "hi",
    "bn-in": "bn", "bn": "bn",
    "gu-in": "gu", "gu": "gu",
    "kn-in": "kn", "kn": "kn",
    "ml-in": "ml", "ml": "ml",
    "mr-in": "mr", "mr": "mr",
    "or-in": "od", "or": "od", "od-in": "od", "od": "od",
    "pa-in": "pa", "pa": "pa",
    "ta-in": "ta", "ta": "ta",
    "te-in": "te", "te": "te",
}

# Words that mean "yes" across all supported languages.
_YES_WORDS: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "sure", "ok", "okay", "correct", "right", "please", "alright",
    # Hindi / Hinglish
    "haan", "ha", "han", "bilkul", "theek", "theekhai", "haa", "hnji", "ji", "haan ji",
    # Tamil
    "aamam", "aama", "seri",
    # Telugu
    "avunu", "aunu", "sare",
    # Kannada
    "houdu", "hauda", "sari",
    # Malayalam
    "athe", "aathe",
    # Marathi
    "ho", "hoy",
    # Native scripts
    "हाँ", "हां", "हान", "जी", "बिल्कुल", "ठीक है", "हाँ जी",
    "হ্যাঁ", "হা",
    "ஆமாம்", "சரி",
    "అవును", "సరే",
    "ಹೌದು", "ಸರಿ",
    "ആതെ", "ശരി",
    "हो", "बरोबर",
    "ਹਾਂ", "ਹਾ",
})

# Words that mean "no" across all supported languages.
_NO_WORDS: frozenset[str] = frozenset({
    "no", "nope", "nah", "not now", "never mind",
    # Hindi
    "nahi", "nahin", "nai", "nhin", "na",
    # Marathi
    "nako", "nahi",
    # Tamil
    "vendam", "vendum",
    # Telugu
    "vaddu", "ledu",
    # Kannada
    "beda", "illa",
    # Malayalam
    "venda", "illa",
    # Bengali
    "na",
    # Native scripts
    "नहीं", "ना", "नाही", "नको",
    "না", "না না",
    "இல்ல", "வேண்டாம்",
    "వద్దు", "లేదు",
    "ಬೇಡ", "ಇಲ್ಲ",
    "വേണ്ട", "ഇല്ല",
    "ਨਹੀਂ", "ਨਾ",
})


def _parse_stt_lang(frame_language) -> str | None:
    """Convert a Pipecat Language enum or raw string to our 2-letter lang code.
    Returns None if not recognised or if English."""
    if frame_language is None:
        return None
    raw = str(frame_language).lower().strip()
    # Handle "Language.HI_IN" enum repr → extract "hi_in" → "hi-in"
    if "." in raw:
        raw = raw.split(".")[-1]
    raw = raw.replace("_", "-")
    return _STT_LANG_MAP.get(raw)


# ── Incomplete-utterance stitching ──────────────────────────────────────────
_CONNECTOR_WORDS = {
    "and", "but", "or", "then", "so", "because", "when", "if",
    "the", "a", "an", "my", "i", "is", "are", "was", "with", "for",
    # Hindi connectors
    "\u0914\u0930", "\u0932\u0947\u0915\u093f\u0928", "\u092e\u0947\u0930\u093e", "\u092e\u0947\u0930\u0940",
}


def _is_likely_incomplete(text: str) -> bool:
    """Return True only if the utterance ends with a connector word (clearly cut off mid-sentence).

    The previous heuristic also flagged any 1-2 word utterance as incomplete, which caused
    a 1.5s stitch delay on every short but complete answer like "24" or "Dental".
    Now only connector-ending utterances (e.g. "my name is", "I want to") are held.
    """
    words = text.strip().split()
    if not words:
        return False
    last = words[-1].lower().rstrip(".,!?;:")
    return last in _CONNECTOR_WORDS

logger = logging.getLogger("booking")

_CONTEXT_SEPARATOR = "\n\n[BOOKING STATE]\n"


class BookingProcessor(FrameProcessor):
    """
    Handles appointment booking state and context injection.

    Critical design: process_frame MUST NOT block. All business logic runs
    in background tasks so the Pipecat pipeline continues processing frames
    (especially audio output frames) while booking state updates happen.
    """

    def __init__(
        self,
        session_id: str,
        clinic_id: str,
        phone: str,
        booking_manager: VoiceBookingManager,
        llm_context: LLMContext,
        clinic_info: dict,
        on_control_message: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_audio_chunk: Optional[Callable[[bytes], Awaitable[None]]] = None,
        sarvam_api_key: str = "",
        tts_model: str = "bulbul:v3",
        tts_speaker: str = "priya",
        on_barge_in: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        super().__init__()
        self.session_id = session_id
        self.clinic_id = clinic_id
        self.phone = phone
        self.booking_manager = booking_manager
        self._llm_context = llm_context
        self._clinic_info = clinic_info
        self._on_control = on_control_message
        self._on_barge_in = on_barge_in

        # Direct TTS (v2 mechanism) — bypasses Pipecat TTS pipeline so
        # broadcast_interruption() from VAD cannot cancel greeting/ACK audio.
        self._on_audio_chunk = on_audio_chunk
        self._tts_client: Optional[AsyncSarvamAI] = (
            AsyncSarvamAI(api_subscription_key=sarvam_api_key)
            if sarvam_api_key else None
        )
        self._tts_model = tts_model
        self._tts_speaker = tts_speaker

        self.language = "en"
        self.turn_count = 0
        self.recent_utterances: list[str] = []

        # Base system prompt (language-independent starting point)
        # Use the smart universal prompt — same regardless of language.
        # (Falls back to LLM context content if already pre-loaded in pipeline_worker.)
        self._base_prompt: str = (
            llm_context.messages[0]["content"]
            if llm_context.messages
            else get_smart_prompt(caller_phone=phone)
        )

        # Context string from PREVIOUS turn's extraction result.
        # Injected into system prompt before push_frame (0ms — just string concat).
        # Updated in background after each turn.
        # Bootstrap value ensures the LLM always has transition guidance even on
        # the very first turn (before any process_turn() has run).
        # Initial context: simple nudge for the first turn only.
        # Subsequent turns use smart_context.build_smart_context() output.
        # NOTE: Do NOT say 'Greet the caller' here — the proactive greeting is already
        # spoken via direct TTS before the first user turn. Saying it here causes the
        # LLM to repeat 'Hello!' on Turn 1.
        self._last_booking_context: str = (
            "[Ready to Help]\n"
            "The caller just connected. The opening greeting has already been spoken. "
            "Do NOT say hello or greet again. Just listen and help with whatever they need."
        )

        # Background extraction task (multiple can run concurrently — do NOT cancel on barge-in)
        self._extraction_task: Optional[asyncio.Task] = None
        # Track all transcript tasks so we can cancel them on cleanup
        self._transcript_tasks: set[asyncio.Task] = set()

        # Lock to serialize process_turn() — concurrent extractions are fine,
        # but process_turn() mutates BookingSession.draft and must not be interleaved.
        self._booking_lock = asyncio.Lock()

        # Utterance stitching state (for cut-off sentences)
        self._pending_text: Optional[str] = None
        self._pending_is_filler: bool = False  # True when _pending_text is a pure filler
        self._stitch_task: Optional[asyncio.Task] = None
        self._STITCH_TIMEOUT_SECS: float = float(__import__("os").getenv("STITCH_TIMEOUT_SECS", "1.5"))

        # Monotonic turn counter — each _process_user_text call increments this.
        # Background tasks capture their seq at spawn; they skip _last_booking_context
        # writes and proactive pushes when a newer turn has already completed.
        self._turn_seq: int = 0

        # Barge-in: flag to interrupt _speak_direct streaming mid-way
        self._direct_tts_interrupted: bool = False

        # Barge-in fragment: last transcript fragment that was interrupted
        # Prepended to the next utterance so broken speech is stitched correctly.
        self._barge_in_fragment: str = ""
        self._current_transcript: str = ""  # tracks the last transcript pushed downstream

        # Language selection state — disabled; smart prompt handles language naturally
        self.awaiting_language_selection: bool = False
        self.language_selection_asked: bool = False

        # Mid-call language offer state (auto-detect from STT):
        # If STT detects user speaking a non-English language, offer to switch once.
        self._pending_lang_confirm: str | None = None   # lang code being offered
        self._lang_offer_context: str | None = None     # context block injected into prompt

        # Tracks previous awaiting_confirmation value so we can detect the
        # False → True transition and push a proactive confirmation TextFrame.
        self._prev_awaiting_confirmation: bool = False

        logger.info(f"[{session_id}] BookingProcessor initialized (non-blocking mode)")

    # ──────────────────────────────────────────────────────────────────────────
    #  Frame routing (MUST be fast — no awaiting business logic here)
    # ──────────────────────────────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TranscriptionFrame):
            # Spawn background task — process_frame returns immediately
            task = asyncio.create_task(self._safe_handle_transcript(frame))
            self._transcript_tasks.add(task)
            task.add_done_callback(self._transcript_tasks.discard)
        elif isinstance(frame, UserStartedSpeakingFrame):
            # ── Barge-in detected ──────────────────────────────────────────
            # 1. Discard any buffered audio in the telephony pacing buffer
            if self._on_barge_in:
                try:
                    await self._on_barge_in()
                except Exception:
                    pass
            # 2. Tell browser to flush all buffered/playing audio
            if self._on_control:
                try:
                    await self._on_control({"type": "stop_audio"})
                except Exception:
                    pass
            # 3. Interrupt any in-flight _speak_direct stream
            self._direct_tts_interrupted = True
            # NOTE: do NOT cancel in-flight extraction — it must complete so
            # booking state accumulates. Cancelling was the root cause of
            # booking never saving to DB.
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    # ──────────────────────────────────────────────────────────────────────────
    #  Direct TTS (v2 mechanism) — bypasses Pipecat pipeline
    # ──────────────────────────────────────────────────────────────────────────

    async def _speak_direct(self, text: str, language: str = "en-IN") -> None:
        """
        Speak `text` directly via Sarvam TTS WebSocket — same mechanism as v2's
        _stream_tts. Sends PCM chunks directly to the browser via on_audio_chunk,
        bypassing Pipecat TTS so broadcast_interruption() from VAD cannot cancel it.

        Barge-in aware: checks _direct_tts_interrupted after each chunk and aborts
        streaming early if user started speaking.
        """
        if not text or not self._tts_client or not self._on_audio_chunk:
            logger.warning(
                f"[{self.session_id}] _speak_direct: missing client or callback, "
                "falling back to TextFrame"
            )
            await self.push_frame(TextFrame(text), FrameDirection.DOWNSTREAM)
            return

        # Reset interruption flag before starting this stream
        self._direct_tts_interrupted = False
        logger.debug(f"[{self.session_id}] Direct TTS [{language}]: {text[:60]}")
        interrupted = False
        _direct_chunk_count = 0
        _direct_total_bytes = 0
        try:
            async with self._tts_client.text_to_speech_streaming.connect(
                model=self._tts_model,
                send_completion_event=True,
            ) as ws:
                await ws.configure(
                    target_language_code=language,
                    speaker=self._tts_speaker,
                    output_audio_codec="linear16",
                    speech_sample_rate=8000,
                )
                await ws.convert(text)
                await ws.flush()
                async for msg in ws:
                    # ── Barge-in check: user started speaking → abort stream ──
                    if self._direct_tts_interrupted:
                        logger.info(
                            f"[{self.session_id}] Direct TTS interrupted by barge-in"
                        )
                        interrupted = True
                        break
                    if isinstance(msg, AudioOutput):
                        pcm_bytes = base64.b64decode(msg.data.audio)
                        _direct_chunk_count += 1
                        _direct_total_bytes += len(pcm_bytes)
                        await self._on_audio_chunk(pcm_bytes)
                    elif isinstance(msg, EventResponse):
                        if (
                            hasattr(msg, "data")
                            and hasattr(msg.data, "event_type")
                            and msg.data.event_type == "final"
                        ):
                            break
        except Exception as e:
            logger.error(f"[{self.session_id}] Direct TTS error: {e}")
        finally:
            logger.info(
                f"[{self.session_id}] Direct TTS done: {_direct_chunk_count} chunks, "
                f"{_direct_total_bytes}B, interrupted={interrupted}"
            )
            # Always signal end-of-TTS so the pacing buffer flushes its tail.
            # On barge-in the buffer was already cleared; this is a no-op for
            # the pacing buffer but resets any downstream state cleanly.
            if not interrupted and self._on_audio_chunk:
                try:
                    await self._on_audio_chunk(b"", end_of_tts=True)
                except Exception as e:
                    logger.debug(f"[{self.session_id}] end_of_tts flush error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    #  Turn handler (runs in background task, NOT in process_frame)
    # ──────────────────────────────────────────────────────────────────────────

    def cancel_pending_tasks(self) -> None:
        """Cancel all in-flight transcript tasks (called on session cleanup)."""
        for task in list(self._transcript_tasks):
            if not task.done():
                task.cancel()
        self._transcript_tasks.clear()

    async def _end_call_after_tts(self) -> None:
        """
        Gracefully end the call after [Booking Confirmed] TTS has had time to play.

        Why this exists: push_frame() from inside a processor does NOT reset the
        PipelineTask idle timer. So after the user's last utterance ("yes"), the
        idle timer counts the entire backend reservation latency + TTS duration.
        If that exceeds idle_timeout_secs, the pipeline cancels mid-TTS and the
        caller hears an abrupt hangup.

        Instead: wait for TTS to finish playing (est. based on text length + buffer),
        then send an end_call control message so FreJun closes the call cleanly.
        The WebSocket disconnect then propagates an EndFrame through the pipeline.
        """
        # Estimate: booking confirmation is ~3-4 sentences (≈12s at normal speech pace).
        # Add 3s buffer for slow connections / audio buffering.
        tts_play_duration = float(os.getenv("BOOKING_CONFIRM_TTS_WAIT_SECS", "15"))
        logger.info(
            f"[{self.session_id}] Waiting {tts_play_duration}s for confirmation TTS to play "
            "before ending call"
        )
        await asyncio.sleep(tts_play_duration)
        logger.info(f"[{self.session_id}] Sending end_call to close session gracefully")
        if self._on_control:
            try:
                await self._on_control({"type": "end_call"})
            except Exception as e:
                logger.warning(f"[{self.session_id}] end_call control error: {e}")

    async def trigger_greeting(self) -> None:
        """
        Proactively speak the opening greeting without waiting for the first
        user utterance. Called by PipecatWorker shortly after the pipeline starts.
        No language selection — the smart prompt handles language naturally.
        """
        if self.language_selection_asked:
            return
        clinic_name = self._clinic_info.get("name", "our clinic")
        greeting = f"{_time_greeting()}! Welcome to {clinic_name}! How may I assist you today?"
        self.language_selection_asked = True
        logger.info(f"[{self.session_id}] Proactive greeting: '{greeting}'")
        if self._on_control:
            try:
                await self._on_control({"type": "response", "text": greeting})
            except Exception:
                pass
        await self._speak_direct(greeting, "en-IN")
        # Inject the greeting into LLM context as an assistant message so the LLM
        # knows the opening has already been spoken and won't repeat 'Hello!' on Turn 1.
        self._llm_context.messages.append({"role": "assistant", "content": greeting})

    async def _safe_handle_transcript(self, frame: TranscriptionFrame):
        """Wraps _handle_transcript with error recovery and a per-turn timeout."""
        _TURN_TIMEOUT = float(__import__("os").getenv("TURN_TIMEOUT_SECS", "40"))
        try:
            await asyncio.wait_for(self._handle_transcript(frame), timeout=_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"[{self.session_id}] Turn handler timed out after {_TURN_TIMEOUT}s — "
                "pushing frame in degraded mode"
            )
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{self.session_id}] Turn error: {e}", exc_info=True)
            # Still push frame so LLM can respond (degraded mode)
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)

    async def _handle_transcript(self, frame: TranscriptionFrame):
        user_text = frame.text.strip()
        if not user_text:
            return

        # ── Noise / background voice gate ─────────────────────────────────────
        # STT returns language_probability and audio_duration on every transcript.
        # Very short + low-confidence = background noise, cough, or someone
        # talking in the background rather than the primary caller.
        _lang_prob = getattr(frame, "language_probability", None) or 1.0
        _audio_dur = getattr(frame, "audio_duration", None) or 999.0
        if _audio_dur < 0.4 and _lang_prob < 0.75:
            logger.info(
                f"[{self.session_id}] Noise gate: dropped '{user_text[:60]}' "
                f"(dur={_audio_dur:.2f}s, prob={_lang_prob:.2f})"
            )
            return

        # ── Utterance stitching: if a pending fragment exists, prepend/replace it ──
        if self._pending_text:
            prev_is_filler = self._pending_is_filler
            if prev_is_filler:
                # Filler was being held; real speech arrived — discard the filler,
                # use only the new real text ("hmm" + "my name is Rahul" → "my name is Rahul")
                logger.info(
                    f"[{self.session_id}] Filler '{self._pending_text}' discarded; "
                    f"real speech arrived: '{user_text}'"
                )
            else:
                # Incomplete utterance stitching — combine both parts
                combined = self._pending_text + " " + user_text
                logger.info(
                    f"[{self.session_id}] Stitched: '{self._pending_text}' + '{user_text}' → '{combined}'"
                )
                user_text = combined
            self._pending_text = None
            self._pending_is_filler = False
            if self._stitch_task and not self._stitch_task.done():
                self._stitch_task.cancel()

        self.turn_count += 1
        logger.info(f"[{self.session_id}] Turn {self.turn_count}: '{user_text[:100]}'")

        if self._on_control:
            try:
                await self._on_control({"type": "transcript", "text": user_text})
            except Exception:
                pass

        # Maintain recent utterances buffer for extraction context
        self.recent_utterances.append(user_text)
        if len(self.recent_utterances) > 5:
            self.recent_utterances.pop(0)

        # ── Mid-call language switch detection ────────────────────────────────
        # Language is LOCKED after the selection phase.
        # A switch is only allowed when the user explicitly asks to change:
        #   "Hindi mein bolo", "speak in Tamil", "switch to Kannada",
        #   "change language", "mujhe hindi mein baat karni hai", etc.
        # Short utterances that merely contain a language keyword (e.g. a name
        # like "Kannada Raju" or a city name) do NOT trigger a switch.
        switch_lang = self._resolve_language_keywords(user_text)
        if switch_lang and switch_lang != self.language:
            _lower = user_text.lower()
            is_explicit_switch = any(
                p in _lower
                for p in (
                    # English explicit phrases
                    "speak in", "switch to", "change to", "change language",
                    "want to speak", "want to talk", "i speak", "let's talk in",
                    "in english", "in hindi", "in tamil", "in telugu", "in kannada",
                    "in malayalam", "in bengali", "in marathi", "in gujarati",
                    "in punjabi", "in odia", "in bangla",
                    # Hindi / Hinglish — "I want to speak in X"
                    "mein bolo", "mein bol", "mein baat", "mein boliye",
                    "mein bolna", "mujhe", "main", "hum",
                    "bhasha badlo", "bhasha change",
                    "bolna chahta", "bolna chahti", "bolna chahte",
                    "baat karna", "baat karni", "baat karte",
                    "\u092d\u093e\u0937\u093e \u092c\u0926\u0932", "\u092d\u093e\u0937\u093e \u092c\u0926\u0932\u094b",
                    "\u092e\u0947\u0902 \u092c\u094b\u0932\u094b", "\u092e\u0947\u0902 \u092c\u093e\u0924",
                    "\u092e\u0941\u091d\u0947", "\u092e\u0948\u0902 \u091a\u093e\u0939",
                    # Bengali — "I want to speak in X" (ami banglay kotha bolte chai)
                    "bolte chai", "bolte chhi", "kotha bolbo", "boltে চাই",
                    "\u0986\u09ae\u09bf", "\u0995\u09a5\u09be \u09ac\u09b2\u09a4\u09c7",
                    # Tamil — "naan X la pesanum" / "X la pesuven"
                    "pesanum", "pesuven", "pesu",
                    # Telugu — "naaku X lo maatladali"
                    "maatladali", "maatladanam",
                    # Kannada — "naanu X nalli maatadabeku"
                    "maatadabeku", "maatadi",
                    # Malayalam — "ente X il saarikshanam"
                    "saarikkanam", "parayan",
                    # Other scripts — "language" / "switch" equivalents
                    "\u092d\u093e\u0937\u093e", "\u09ad\u09be\u09b7\u09be",
                    "\u0bae\u0bca\u0bb4\u0bbf \u0bae\u0bbe\u0bb1\u0bc1",
                    "\u0c2d\u0c3e\u0c37 \u0c2e\u0c3e\u0c30\u0c4d\u0c1a\u0c41",
                    "\u0cad\u0cbe\u0cb7\u0cc6 \u0cac\u0ca6\u0cb2\u0cbf",
                    "\u0d2d\u0d3e\u0d37 \u0d2e\u0d3e\u0d31\u0d4d\u0d31\u0d41\u0d15",
                )
            )
            if is_explicit_switch:
                # User directly stated their language preference → silent switch, no yes/no needed.
                # Clear any pending auto-offer.
                self._pending_lang_confirm = None
                self._lang_offer_context = None
                self._apply_language(switch_lang)
                logger.info(f"[{self.session_id}] Explicit language preference → {switch_lang} (silent, no confirmation)")
                # Fall through: LLM responds naturally in the new language.

        # ── Auto language-offer: detect STT language, offer once if != English ──
        # Trigger ONLY when the utterance is a proper sentence (≥6 words), to avoid
        # false positives from short answers, names, or single words.
        # English is the default. If STT reports a non-English language consistently,
        # inject a [Language Offer] block so the LLM asks naturally (one turn only).
        # If the user confirms → silently switch. If they decline → stay in English.
        _STT_OFFER_MIN_WORDS = 6  # don't offer on short/ambiguous utterances

        if self._pending_lang_confirm:
            # User is responding to a language offer — check for yes/no.
            _resp = user_text.lower().strip().rstrip(".,!?;:")
            _resp_words = set(_resp.split())
            if _resp_words & _YES_WORDS or _resp in _YES_WORDS:
                _offered = self._pending_lang_confirm
                self._pending_lang_confirm = None
                self._lang_offer_context = None
                self._apply_language(_offered)
                logger.info(
                    f"[{self.session_id}] User confirmed language switch to {_offered} — "
                    "continuing in same flow"
                )
                # Fall through: LLM will respond naturally in the new language
            elif _resp_words & _NO_WORDS or _resp in _NO_WORDS:
                logger.info(
                    f"[{self.session_id}] User declined language switch — staying in {self.language}"
                )
                self._pending_lang_confirm = None
                self._lang_offer_context = None
                # Fall through: continue in current language
            # else: ambiguous — keep offer active, LLM will re-ask gently next turn

        elif self.language == "en" and not self._pending_lang_confirm:
            # Not yet in an offer cycle — check if STT reported a non-English language
            # Only trigger if the utterance is long enough to be a real sentence.
            detected_lang = _parse_stt_lang(getattr(frame, "language", None))
            _word_count = len(user_text.split())
            if detected_lang and detected_lang != "en" and _word_count >= _STT_OFFER_MIN_WORDS:
                lang_name = _LANG_NAMES.get(detected_lang, detected_lang)
                logger.info(
                    f"[{self.session_id}] STT detected '{detected_lang}' on {_word_count}-word utterance "
                    "— offering language switch"
                )
                self._pending_lang_confirm = detected_lang
                self._lang_offer_context = (
                    f"[Language Offer]\n"
                    f"The caller appears to be speaking {lang_name}. "
                    f"In ONE friendly sentence, ask if they'd like to continue in {lang_name}. "
                    f"Do NOT ask any booking question this turn — just ask about the language preference."
                )

        # ── Smart filler hold: "hmm", "uh", "ok" → wait for continuation ──
        # Instead of immediately responding, hold the filler for _STITCH_TIMEOUT_SECS.
        # If real speech follows within that window, discard the filler and process
        # only the real text. If no continuation arrives, fire a context-aware re-prompt.
        if _PURE_FILLER_RE.match(user_text):
            logger.info(
                f"[{self.session_id}] Filler held for continuation: '{user_text}'"
            )
            self._pending_text = user_text
            self._pending_is_filler = True
            if self._stitch_task and not self._stitch_task.done():
                self._stitch_task.cancel()
            self._stitch_task = asyncio.create_task(
                self._stitch_timeout(user_text, frame)
            )
            return

        # ── Incomplete utterance stitching: hold short/cut-off sentences ──
        if _is_likely_incomplete(user_text):
            logger.info(
                f"[{self.session_id}] Incomplete utterance, holding for continuation: '{user_text}'"
            )
            self._pending_text = user_text
            self._pending_is_filler = False
            if self._stitch_task and not self._stitch_task.done():
                self._stitch_task.cancel()
            self._stitch_task = asyncio.create_task(
                self._stitch_timeout(user_text, frame)
            )
            return

        await self._process_user_text(user_text, frame)

    async def _stitch_timeout(self, text: str, frame: TranscriptionFrame):
        """Fire after STITCH_TIMEOUT_SECS if no continuation arrives."""
        await asyncio.sleep(self._STITCH_TIMEOUT_SECS)
        if self._pending_text == text:
            was_filler = self._pending_is_filler
            self._pending_text = None
            self._pending_is_filler = False
            if was_filler:
                # Filler timed out with no continuation — fire context-aware re-prompt.
                # This re-asks the current unanswered booking question instead of
                # saying a generic "Sure, go ahead" which would confuse the user.
                logger.info(
                    f"[{self.session_id}] Filler stitch timeout — context-aware bridge for: '{text}'"
                )
                await self._bridge_filler_response()
            else:
                logger.info(f"[{self.session_id}] Stitch timeout — processing as-is: '{text}'")
                await self._process_user_text(text, frame)

    async def _bridge_filler_response(self) -> None:
        """
        Send a context-aware gentle re-prompt based on what booking field is
        currently missing. Called after a filler ("hmm", "ok", etc.) is held
        for the stitch window and no real speech follows.

        This replaces the generic "Sure, go ahead." with a question that
        moves the conversation forward without sounding robotic.
        """
        lang = self.language
        draft = self.booking_manager.draft if hasattr(self.booking_manager, 'draft') else {}

        # Determine which booking field is being awaited
        if not draft.get("name"):
            re_prompts = {
                "en": "Could you tell me your name, please?",
                "hi": "आपका नाम क्या है?",
                "bn": "আপনার নাম কি?",
                "ta": "உங்க பெயரு சொல்லுங்களோ?",
                "te": "మీ పేరు చెప్పగలరా?",
                "kn": "ನಿಮ್ಮ ಹೆಸರು ಹೇಳಿ?",
                "ml": "ആപളുടെ പേര്‍ പറയാമോ?",
                "mr": "तुमचं नाव सांगताका?",
                "gu": "તમારું નામ શું છે?",
                "pa": "ਤੁਹਾਡਾ ਨਾਮ ਦੱਸો?",
                "od": "ଆପଣଙ୍କ ନାମ କୁହନ୍ତୁ?",
            }
        elif not draft.get("age"):
            re_prompts = {
                "en": "And how old are you?",
                "hi": "आपकी उम्र क्या है?",
                "bn": "আপনার বয়স কত?",
                "ta": "உங்களுக்கு எத்தனை வயது?",
                "te": "మీ వయసు ఎంత?",
                "kn": "ನಿಮ್ಮ ವಯಸ್ಸೆಷ್ಟ?",
                "ml": "നിങ്ങളുടെ പ്രായം എത്ര?",
                "mr": "तुमचं वय किती?",
                "gu": "તમારી ામર કેટલી છે?",
                "pa": "ਤੁਹਾਡી ਉਮਰ ਕਿੰਨੀ ਹੈ?",
                "od": "ଆପଣଙ୍କ ବୟସ କେତ?",
            }
        elif not draft.get("department"):
            re_prompts = {
                "en": "What health concern brings you in today?",
                "hi": "आपको किस समस्या के लिए डॉक्टर से मिलना है?",
                "bn": "আপনার কোন স্বাস্থ্য সমস্যা?",
                "ta": "என்னஉடல் பிரச்சினைக்காக வருகிறீர்கள்?",
                "te": "మీకు ఏమి అనిపిస్తుంది?",
                "kn": "ಯಾವ ಆರೋಗ್ಯ ಸಮಸ್ಯೆಗಾಗಿ ಬಂದಿದ್ದೀರಿ?",
                "ml": "എന്ത് ആരോഗ്യ പ്രശ്നമാണ്‍?",
                "mr": "कोणता त्रास आहे?",
                "gu": "કી તકલીફ છે?",
                "pa": "ਕੀ ਸਿਹਤ ਸਮੱਸਿਆ ਹੈ?",
                "od": "କଣ୍ଟ୍ରୋଲ କରନ୍ତା?",
            }
        elif not draft.get("date"):
            re_prompts = {
                "en": "What date would you like for the appointment?",
                "hi": "अपॉइंटमेंट किस तारीख को चाहिए?",
                "bn": "কোন তারিখে অ্যাপায়ন্টমেন্ট চান?",
                "ta": "எந்த தேதி வருகிறீர்கள்?",
                "te": "ఏ తేదిన వచ్చేట్టు అక్కడికి రాగలరు?",
                "kn": "ಯಾವ ದಿನಾಂಕಕ್ಕೆ ಬರಬಹುದು?",
                "ml": "ഏത് തീയതിയാണ്‍ ആവശ്യം?",
                "mr": "कोणत्या तारखेला येणार?",
                "gu": "કયા તારીખે આવવા છો?",
                "pa": "ਕਿਹੜી ਤਰੀਕ ਰਾਹਿ ਕਰਨਾ ਹੈ?",
                "od": "କେଉଁ ତାରିଖରେ ଆସିବା?",
            }
        elif not draft.get("time"):
            re_prompts = {
                "en": "What time works best for you?",
                "hi": "आपके लिए कौन सा समय ठीक रहेगा?",
                "bn": "কোন সময়ে আসতে পারবেন?",
                "ta": "எந்த நேரம் வருகிறீர்கள்?",
                "te": "ఏ సమయం అనుకూలంగా ఉంటుంది?",
                "kn": "ಯಾವ ವೇಳೆಗೆ ಬರಬಹುದು?",
                "ml": "ഏത് സമയല്‍ സൗകര്യമാണ്‍?",
                "mr": "किती वाजता क्वी?",
                "gu": "કેટલા વાગ્યે આવવાનું છે?",
                "pa": "ਕਿਹੜે ਵੇਲે ਆਵોਗે?",
                "od": "କେଉଁ ସମଯରେ ଆସିବା?",
            }
        else:
            # All fields collected — or unknown state; gentle listening prompt
            re_prompts = {
                "en": "Please go ahead, I'm listening.",
                "hi": "हाँ, बताइए।",
                "bn": "হ্যাঁ, বলুন।",
                "ta": "சரி, சொல்லுங்கள்.",
                "te": "అవును, చెప్పండి.",
                "kn": "ಹೌದು, ಹೇಳಿ.",
                "ml": "ശരി, പറയാമോ.",
                "mr": "हो, सांगा.",
                "gu": "હા, કહો.",
                "pa": "ਹાਂ, ਦੱਸો.",
                "od": "ହଁ, କୁହନ୍ତୁ.",
            }

        bridge = re_prompts.get(lang, re_prompts["en"])
        logger.info(f"[{self.session_id}] Context-aware filler bridge [{lang}]: '{bridge}'")
        if self._on_control:
            try:
                await self._on_control({"type": "response", "text": bridge})
            except Exception:
                pass
        # Send directly through TTS so it plays at the right language
        await self.push_frame(TextFrame(bridge), FrameDirection.DOWNSTREAM)

    def _build_confirmation_text(self, draft: dict, time_just_selected: bool = False) -> str:
        """
        Build a confirmation readback sentence from the draft fields.

        This is generated in Python — not by the LLM — so it always uses
        the exact draft values regardless of what was discussed in history.
        Called proactively when awaiting_confirmation first transitions to True.

        When time_just_selected=True (user just picked a specific slot),
        leads with "X is available!" for a more natural confirmation flow.
        """
        name = draft.get("name", "")
        age  = draft.get("age", "")
        dept = draft.get("department", "")
        date = draft.get("date", "")
        time = draft.get("time", "")
        lang = self.language

        if time_just_selected:
            templates: dict[str, str] = {
                "en": (
                    f"{time} is available! Just to confirm — {name}, age {age}, "
                    f"{dept} department, on {date} at {time}. Shall I go ahead and book this?"
                ),
                "hi": (
                    f"{time} available है! एक बार confirm करते हैं — {name}, उम्र {age}, "
                    f"{dept} department, {date} को {time} बजे। क्या book करूँ?"
                ),
                "bn": (
                    f"{time} available! একবার confirm করি — {name}, বয়স {age}, "
                    f"{dept} department, {date} তে {time}। বুক করব?"
                ),
                "ta": (
                    f"{time} available! Confirm பண்ணலாமா — {name}, வயது {age}, "
                    f"{dept} department, {date} அன்று {time}க்கு. Book பண்ணட்டுமா?"
                ),
                "te": (
                    f"{time} available! Confirm చేద్దామా — {name}, వయసు {age}, "
                    f"{dept} department, {date} న {time} కి. Book చేయనా?"
                ),
                "kn": (
                    f"{time} available! Confirm ಮಾಡೋಣ — {name}, ವಯಸ್ಸು {age}, "
                    f"{dept} department, {date} ರಂದು {time} ಕ್ಕೆ. Book ಮಾಡಲಾ?"
                ),
                "ml": (
                    f"{time} available! Confirm ചെയ്യട്ടെ — {name}, പ്രായം {age}, "
                    f"{dept} department, {date} ന്‍ {time} ന്. Book ചെയ്യട്ടെ?"
                ),
                "mr": (
                    f"{time} available आहे! Confirm करूया — {name}, वय {age}, "
                    f"{dept} department, {date} ला {time} वाजता. Book करू का?"
                ),
                "gu": (
                    f"{time} available છે! Confirm કરીએ — {name}, ઉંમર {age}, "
                    f"{dept} department, {date} ના {time} વાગ્યે. Book કરું?"
                ),
                "pa": (
                    f"{time} available ਹੈ! Confirm ਕਰੀਏ — {name}, ਉਮਰ {age}, "
                    f"{dept} department, {date} ਨੂੰ {time} ਵਜੇ. Book ਕਰਾਂ?"
                ),
                "od": (
                    f"{time} available! Confirm କରୁ — {name}, ବୟସ {age}, "
                    f"{dept} department, {date} ରେ {time}ରେ. Book କରିବି?"
                ),
            }
        else:
            templates = {
                "en": (
                    f"Just to confirm — {name}, age {age}, {dept} department, "
                    f"on {date} at {time}. Shall I go ahead and book this?"
                ),
                "hi": (
                    f"एक बार confirm करते हैं — {name}, उम्र {age}, {dept} department, "
                    f"{date} को {time} बजे। क्या book करूँ?"
                ),
                "bn": (
                    f"একবার confirm করি — {name}, বয়স {age}, {dept} department, "
                    f"{date} তে {time}। বুক করব?"
                ),
                "ta": (
                    f"Confirm பண்ணலாமா — {name}, வயது {age}, {dept} department, "
                    f"{date} அன்று {time}க்கு. Book பண்ணட்டுமா?"
                ),
                "te": (
                    f"Confirm చేద్దామా — {name}, వయసు {age}, {dept} department, "
                    f"{date} న {time} కి. Book చేయనా?"
                ),
                "kn": (
                    f"Confirm ಮಾಡೋಣ — {name}, ವಯಸ್ಸು {age}, {dept} department, "
                    f"{date} ರಂದು {time} ಕ್ಕೆ. Book ಮಾಡಲಾ?"
                ),
                "ml": (
                    f"Confirm ചെയ്യട്ടെ — {name}, പ്രായം {age}, {dept} department, "
                    f"{date} ന്‍ {time} ന്. Book ചെയ്യട്ടെ?"
                ),
                "mr": (
                    f"Confirm करूया — {name}, वय {age}, {dept} department, "
                    f"{date} ला {time} वाजता. Book करू का?"
                ),
                "gu": (
                    f"Confirm કરીએ — {name}, ઉંમર {age}, {dept} department, "
                    f"{date} ના {time} વાગ્યે. Book કરું?"
                ),
                "pa": (
                    f"Confirm ਕਰੀਏ — {name}, ਉਮਰ {age}, {dept} department, "
                    f"{date} ਨੂੰ {time} ਵਜੇ. Book ਕਰਾਂ?"
                ),
                "od": (
                    f"Confirm କରୁ — {name}, ବୟସ {age}, {dept} department, "
                    f"{date} ରେ {time}ରେ. Book କରିବି?"
                ),
            }
        return templates.get(lang, templates["en"])

    async def _process_user_text(self, user_text: str, frame: TranscriptionFrame):
        """Core per-turn logic: patch context, push frame, spawn background extraction."""
        # ── Step 1: Patch system prompt with LAST TURN'S cached context (0ms) ──
        self._patch_context()

        # ── Step 2: Track current transcript for barge-in fragment stitching ──
        self._current_transcript = user_text

        # ── Step 3: Increment turn sequence — used to detect stale background tasks ──
        self._turn_seq += 1
        my_seq = self._turn_seq

        # ── Step 4: Push TranscriptionFrame downstream — LLM starts IMMEDIATELY ──
        await self.push_frame(frame, FrameDirection.DOWNSTREAM)

        # ── Step 5: Spawn extraction + booking update in BACKGROUND ──
        # Do NOT cancel the previous task — let it complete so the draft
        # accumulates fields from all turns (was the booking-not-saving bug).
        self._extraction_task = asyncio.create_task(
            self._update_booking_context(user_text, my_seq)
        )

    # ──────────────────────────────────────────────────────────────────────────
    #  Context patching (called synchronously before push_frame, takes 0ms)
    # ──────────────────────────────────────────────────────────────────────────

    def _patch_context(self):
        """
        Inject cached booking context into LLMContext system prompt.

        Directly mutates llm_context.messages[0] — no frames, no async, no latency.
        Called BEFORE push_frame so LLM sees the updated context immediately.

        Also repairs consecutive user messages caused by interrupted LLM responses
        (when LLM is cancelled before producing tokens, pipecat skips adding an
        empty assistant message, leaving two back-to-back user turns that the
        Sarvam API rejects with HTTP 400 "turns must alternate").
        """
        # Build the booking-state portion of the system prompt.
        # Language offer (if active) takes EXCLUSIVE priority — suppress all booking
        # context so the LLM focuses solely on asking about the language preference.
        # Once the offer is resolved, normal booking context resumes.
        if self._lang_offer_context:
            new_content = self._base_prompt + _CONTEXT_SEPARATOR + self._lang_offer_context
        elif self._last_booking_context:
            new_content = self._base_prompt + _CONTEXT_SEPARATOR + self._last_booking_context
        else:
            new_content = self._base_prompt

        if self._llm_context.messages:
            self._llm_context.messages[0] = {"role": "system", "content": new_content}
        else:
            self._llm_context.messages = [{"role": "system", "content": new_content}]

        # ── Repair alternation: insert placeholder between consecutive user turns ──
        msgs = self._llm_context.messages
        i = 1  # skip system message at index 0
        while i < len(msgs) - 1:
            if msgs[i].get("role") == "user" and msgs[i + 1].get("role") == "user":
                msgs.insert(i + 1, {"role": "assistant", "content": "Okay."})
                i += 2  # skip over the just-inserted placeholder
            else:
                i += 1

        # ── End-guard: previous LLM was cancelled/0-chunk → no assistant added ──
        # Without this, next push_frame(TranscriptionFrame) creates consecutive users → HTTP 400.
        if len(msgs) > 1 and msgs[-1].get("role") == "user":
            msgs.append({"role": "assistant", "content": "Okay."})

    # ──────────────────────────────────────────────────────────────────────────
    #  Background extraction (parallel with LLM streaming)
    # ──────────────────────────────────────────────────────────────────────────

    async def _update_booking_context(self, user_text: str, turn_seq: int):
        """
        Run extraction + process_turn in background AFTER push_frame.

        LLM is already streaming while this runs. The result is cached
        in _last_booking_context and will be used on the NEXT turn.

        This saves ~2.8s per turn (no blocking before LLM start).

        turn_seq: sequence number captured at spawn. If a newer turn has already
        completed by the time we finish, we skip _last_booking_context writes and
        proactive pushes to avoid overwriting fresher state with stale results.
        """
        try:
            clinic_depts = self._clinic_info.get("departments", [])
            recent = self.recent_utterances[:-1] if len(self.recent_utterances) > 1 else None

            # Snapshot offered times and prev draft time BEFORE extraction/process_turn
            _session_pre = self.booking_manager.sessions.get(self.session_id)
            offered_times = list(getattr(_session_pre, "last_offered_times", []) or [])
            prev_draft_time = _session_pre.draft.get("time") if _session_pre else None
            prev_draft = dict(_session_pre.draft) if _session_pre else {}
            prev_awaiting_confirmation = bool(getattr(_session_pre, "awaiting_confirmation", False))

            # Build awaiting_state hint so extraction knows what to focus on
            _awaiting_state: str | None = None
            if _session_pre:
                if _session_pre.awaiting_age_confirmation:
                    _awaiting_state = f"awaiting age confirmation — user is responding to: 'Is the age {_session_pre.draft.get('age')} correct?'"
                elif _session_pre.awaiting_name_confirmation:
                    _awaiting_state = f"awaiting name confirmation — user is responding to: 'Is the name {_session_pre.draft.get('name')} correct?'"
                elif _session_pre.awaiting_confirmation:
                    _awaiting_state = "awaiting booking confirmation — user is responding to 'Shall I go ahead and book?'"
                elif _session_pre.awaiting_reschedule_confirm:
                    _awaiting_state = "awaiting reschedule or new booking choice"
                elif _session_pre.awaiting_time_clarification:
                    _awaiting_state = f"awaiting AM/PM clarification for time '{_session_pre.time_clarification_value}'"
                elif getattr(_session_pre, "awaiting_contact_confirmation", False):
                    _awaiting_state = f"awaiting contact number confirmation — user is choosing whether to use {_session_pre.phone} or provide a different number"
                elif getattr(_session_pre, "awaiting_channel_selection", False):
                    _awaiting_state = "awaiting communication channel selection — user is choosing WhatsApp, SMS, email, or voice call"
                elif offered_times:
                    _awaiting_state = f"awaiting slot selection from offered times: {', '.join(offered_times)}"
                else:
                    from booking.manager import FIELD_ORDER
                    missing = [f for f in FIELD_ORDER if not _session_pre.draft.get(f)]
                    if missing:
                        _awaiting_state = f"collecting missing field: {missing[0]}"

            extraction = await extract_booking_info(
                user_text=user_text,
                current_draft=self.booking_manager.draft,
                clinic_departments=clinic_depts or None,
                recent_utterances=recent,
                offered_times=offered_times or None,
                awaiting_state=_awaiting_state,
                today_date=datetime.now(ZoneInfo(os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata"))).strftime("%-d %b"),
            )
            # Attach raw user_text so contact confirmation handlers can do keyword matching
            extraction["_user_text"] = user_text
            logger.info(
                f"[{self.session_id}] Extraction: intent={extraction.get('intent')} "
                f"confirm={extraction.get('confirm')}"
            )

            # Serialize process_turn() with a lock — concurrent extractions
            # are fine (they're reads), but process_turn() mutates BookingSession
            # and must not be interleaved across overlapping turns.
            async with self._booking_lock:
                result = await self.booking_manager.process_turn(
                    session_id=self.session_id,
                    clinic_id=self.clinic_id,
                    phone=self.phone,
                    user_text=user_text,
                    extraction=extraction,
                    lang=self.language,
                )

            # Confirm draft was persisted in the session after process_turn()
            _session = self.booking_manager.sessions.get(self.session_id)
            logger.info(
                f"[{self.session_id}] post-process_turn draft="
                f"{_session.draft if _session else '(session not found)'}"
            )

            context_text = result.get("context", "")

            # ── Stale-turn guard ──────────────────────────────────────────
            # If a newer turn has already completed, skip all side-effects
            # (context write + proactive push) to avoid overwriting fresh state.
            is_stale = self._turn_seq > turn_seq
            if is_stale:
                logger.debug(
                    f"[{self.session_id}] Stale turn seq={turn_seq} "
                    f"(current={self._turn_seq}) — skipping context write & proactive push"
                )

            # Snapshot stale context BEFORE overwriting — used for TextFrame guard below
            stale_context = self._last_booking_context

            if context_text and not is_stale:
                self._last_booking_context = context_text
                logger.debug(
                    f"[{self.session_id}] Context cached for next turn "
                    f"({len(context_text)} chars)"
                )

            # ── Proactive confirmation readback ─────────────────────────────
            # When awaiting_confirmation transitions False → True, the LLM for
            # THIS turn fired with STALE context (e.g. [Available Slots]) and
            # will say "Perfect!" not the confirmation question.  Push the
            # confirmation text directly from the draft so:
            #   1. The agent speaks immediately without waiting for user input.
            #   2. The time/details come from draft — never hallucinated.
            #
            # GUARDS:
            # - Skip if this is a stale turn (newer turn already completed).
            # - Don't fire when stale context was presenting slot choices WITHOUT
            #   a time change — LLM is already handling those with a coherent ack;
            #   concurrent TextFrame causes contradictions.
            # - DO fire even if _prev_awaiting_confirmation=True when draft.time
            #   just changed (user picked a different slot mid-confirmation).
            _CHOICE_CONTEXT_MARKERS = (
                "[Change Request]",
                "[Available Slots]",
                "[Slot Unavailable]",
                "[No Slots",
            )
            stale_was_offering = any(m in stale_context for m in _CHOICE_CONTEXT_MARKERS)

            _session = self.booking_manager.sessions.get(self.session_id)
            now_awaiting = bool(_session and _session.awaiting_confirmation)
            current_draft_time = _session.draft.get("time") if _session else None
            time_changed = bool(
                current_draft_time and prev_draft_time != current_draft_time
            )

            # ── Detect field correction during confirmation ──────────────────
            _CONFIRMABLE = ["name", "age", "department", "date", "time"]
            current_draft_post = _session.draft if _session else {}
            field_corrected_during_confirmation = (
                prev_awaiting_confirmation and now_awaiting and _session
                and any(
                    current_draft_post.get(f) and prev_draft.get(f)
                    and current_draft_post.get(f) != prev_draft.get(f)
                    for f in _CONFIRMABLE
                )
            )
            if field_corrected_during_confirmation:
                _session.confirmation_presented_to_llm = False

            # When the LLM already acknowledged a slot pick ("Perfect, 6 PM it is."),
            # the stale context was [Available Slots] and time just changed.
            # We still want to push the confirmation question — but without "X is available!"
            # since the LLM just said that. Skipping it entirely would leave the user
            # waiting forever for a confirmation that never comes.
            llm_already_handled_slot_pick = stale_was_offering and time_changed

            should_push_confirmation = (
                not is_stale
                and now_awaiting
                and _session
                and not _session.confirmation_presented_to_llm
                and (
                    field_corrected_during_confirmation
                    or (
                        (not stale_was_offering or time_changed)
                        and (not self._prev_awaiting_confirmation or time_changed)
                    )
                )
            )

            if should_push_confirmation:
                _CORRECTION_PREFIX = {
                    "en": "Got it, updated!",
                    "hi": "ठीक है, अपडेट कर दिया।",
                    "bn": "ঠিক আছে, আপডেট করলাম।",
                    "ta": "சரி, அப்டேட் பண்ணிட்டேன்.",
                    "te": "సరే, అప్‌డేట్ చేశాను.",
                    "kn": "ಸರಿ, ಅಪ್‌ಡೇಟ್ ಮಾಡಿದೆ.",
                    "ml": "ശരി, അപ്ഡേറ്റ് ചെയ്തു.",
                    "mr": "ठीक आहे, अपडेट केले.",
                    "gu": "ઠીક છે, અપડેટ કર્યું.",
                    "pa": "ਠੀਕ ਹੈ, ਅੱਪਡੇਟ ਕੀਤਾ।",
                    "od": "ଠيক ଅଛي, ଅपডেট ହোইছি।",
                }
                # When LLM already acked the slot pick, skip the "X is available!" lead-in
                # (LLM already said "Perfect, X it is.") to avoid sounding repetitive.
                time_just_selected = time_changed and not llm_already_handled_slot_pick
                conf_text = self._build_confirmation_text(
                    _session.draft, time_just_selected=time_just_selected
                )
                if field_corrected_during_confirmation:
                    prefix = _CORRECTION_PREFIX.get(self.language, "Got it, updated!")
                    conf_text = prefix + " " + conf_text
                _session.confirmation_presented_to_llm = True
                logger.info(
                    f"[{self.session_id}] Proactive confirmation readback "
                    f"(time_changed={time_changed}, llm_acked_slot={llm_already_handled_slot_pick}): "
                    f"'{conf_text}'"
                )
                # Eagerly write confirmation context so next LLM turn sees [Awaiting Confirmation]
                if context_text:
                    self._last_booking_context = context_text
                await self.push_frame(TextFrame(conf_text), FrameDirection.DOWNSTREAM)
            self._prev_awaiting_confirmation = now_awaiting
            # ───────────────────────────────────────────────────────────────

            if result.get("end_call"):
                logger.info(f"[{self.session_id}] Booking complete — scheduling graceful call end")
                asyncio.create_task(self._end_call_after_tts())

        except asyncio.CancelledError:
            logger.info(f"[{self.session_id}] Background extraction cancelled (barge-in)")
        except Exception as e:
            logger.error(f"[{self.session_id}] Background context update error: {e}", exc_info=True)
        finally:
            self._extraction_task = None

    # ──────────────────────────────────────────────────────────────────────────
    #  Language detection (synchronous — no LLM calls)
    # ──────────────────────────────────────────────────────────────────────────

    def _detect_language(self, text: str) -> Optional[str]:
        """Detect language from script/keyword patterns. No API calls."""
        # Hindi
        if any(c in text for c in "अआइईउऊएऐओऔकखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह"):
            return "hi"
        # Bengali
        if any(c in text for c in "অআইঈউঊএঐওঔকখগঘচছজঝটঠডঢণতথদধনপফবভমযরলবশষসহ"):
            return "bn"
        # Tamil (no spaces — spaces in the old string caused false-positives for English)
        if any(c in text for c in "அஆஇஈஉஊஎஏஐஒஓஔகங்சஞடணதநபமயரலவழளறன"):
            return "ta"
        # Telugu
        if any(c in text for c in "అఆఇఈఉఊఎఏఐఒఓఔకఖగఘచఛజఝటఠడఢణతథదధనపఫబభమయరలవశషస"):
            return "te"
        # Kannada
        if any(c in text for c in "ಅಆಇಈಉಊಎಏಐಒಓಔಕಖಗಘಚಛಜಝಟಠಡಢಣತಥದಧನಪಫಬಭಮಯರಲವಶಷಸ"):
            return "kn"
        # Malayalam
        if any(c in text for c in "അആഇഈഉഊഎഏഐഒഓഔകഖഗഘചഛജഝടഠഡഢണതഥദധനപഫബഭമയരലവശഷസ"):
            return "ml"
        # Gujarati
        if any(c in text for c in "અઆઇઈઉઊએઐઓઔકખગઘચછજઝટઠડઢણતથદધનપફબભમયરલવ"):
            return "gu"
        # Marathi (same script as Hindi but keyword check)
        if any(w in text for w in ["होय", "नाही", "मी", "काय", "आहे", "मला"]):
            return "mr"
        # Punjabi
        if any(c in text for c in "ਅਆਇਈਉਊਏਐਓਔਕਖਗਘਚਛਜਝਟਠਡਢਣਤਥਦਧਨਪਫਬਭਮਯਰਲਵਸਹ"):
            return "pa"
        # Odia
        if any(c in text for c in "ଅଆଇଈଉଊଏଐଓଔକଖଗଘଚଛଜଝଟଠଡଢଣତଥଦଧନପଫବଭମଯରଲ"):
            return "od"
        return "en"

    def _resolve_language_keywords(self, text: str) -> Optional[str]:
        """
        Keyword-only match — no script detection.

        Checks if any _LANGUAGE_KEYWORDS entry appears as a substring in the
        (lowercased) text. Returns the language code or None.
        Uses keyword-only matching to avoid false-positives where script
        detection would trigger on greetings like "Hello" or "Namaste".
        """
        text_lower = text.lower()
        for keyword, code in _LANGUAGE_KEYWORDS.items():
            if keyword.lower() in text_lower:
                return code
        return None

    def _stt_lang_code(self, frame: TranscriptionFrame) -> Optional[str]:
        """
        Extract language code from the STT frame's language_code field.

        Returns a 2-letter code (e.g. "en", "hi") if available, else None.
        Used as a reliable fallback when keyword matching fails — the STT
        has already done probabilistic language detection on the audio.
        """
        _VALID = {"en", "hi", "bn", "ta", "te", "kn", "ml", "mr", "gu", "pa", "od"}
        stt_lang = getattr(frame, "language", None)
        if stt_lang and "-" in stt_lang:
            candidate = stt_lang.split("-")[0].lower()
            if candidate in _VALID:
                return candidate
        return None

    def _apply_language(self, lang: str):
        """Set language code, rebuild system prompt, and update TTS language."""
        prev = self.language
        self.language = lang
        clinic_name = self._clinic_info.get("name", "")
        depts = self._clinic_info.get("departments", [])
        hours = self._clinic_info.get("working_hours", {})
        if isinstance(hours, dict):
            hours = ", ".join(f"{k}: {v}" for k, v in hours.items())
        # Universal prompt — language param ignored, same prompt for all languages.
        # TTS language is updated below so speech is in the caller's language.
        self._base_prompt = get_smart_prompt(
            clinic_name=clinic_name or None,
            departments=depts or None,
            working_hours=hours or None,
            caller_phone=self.phone,
        )
        logger.info(f"[{self.session_id}] Language applied: {prev} → {lang}")
        # Update TTS language so subsequent speech is spoken in the chosen language
        pipecat_lang = _LANG_TO_PIPECAT.get(lang, Language.EN_IN)
        asyncio.create_task(
            self.push_frame(TTSUpdateSettingsFrame(delta=TTSSettings(language=pipecat_lang)))
        )

