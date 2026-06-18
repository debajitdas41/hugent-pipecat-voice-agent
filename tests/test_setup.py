"""
Simple test script to verify Pipecat + Sarvam integration works

Tests:
1. Import all required modules
2. Load environment variables
3. Initialize Sarvam services
4. Create a minimal pipeline (without running)
"""
import sys
import os

# Add project root to path (tests/ is one level below root)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 60)
print("Pipecat-Sarvam Integration Test")
print("=" * 60)

# Test 1: Import modules
print("\n[1/5] Testing imports...")
try:
    # Import from specific modules (not from __init__)
    from pipecat.services.sarvam.stt import SarvamSTTService
    from pipecat.services.sarvam.tts import SarvamTTSService
    from pipecat.services.sarvam.llm import SarvamLLMService
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask, PipelineParams
    from pipecat.frames.frames import AudioRawFrame, LLMMessagesAppendFrame, EndFrame
    print("✓ Pipecat imports successful")
except ImportError as e:
    print(f"✗ Pipecat import failed: {e}")
    sys.exit(1)

try:
    from services.backend_client import BackendClient
    from booking.manager import VoiceBookingManager
    from prompts.loader import get_system_prompt
    print("✓ Business logic imports successful")
except ImportError as e:
    print(f"✗ Business logic import failed: {e}")
    sys.exit(1)

try:
    from processors.audio_input import AudioInputProcessor
    from processors.audio_output import AudioOutputProcessor
    from processors.booking_processor import BookingProcessor
    from processors.assistant_capture import AssistantCaptureProcessor
    print("✓ Custom processor imports successful")
except ImportError as e:
    print(f"✗ Custom processor import failed: {e}")
    sys.exit(1)

# Test 2: Load environment
print("\n[2/5] Loading environment...")
from dotenv import load_dotenv
load_dotenv()

sarvam_key = os.getenv("SARVAM_API_KEY")
if sarvam_key and len(sarvam_key) > 10:
    print(f"✓ SARVAM_API_KEY loaded ({sarvam_key[:8]}...)")
else:
    print("✗ SARVAM_API_KEY not set or invalid")
    print("  Please copy .env.example to .env and add your API key")
    sys.exit(1)

backend_url = os.getenv("BACKEND_API_BASE_URL", "http://localhost:8000")
print(f"✓ BACKEND_API_BASE_URL: {backend_url}")

# Test 3: Initialize Sarvam services
print("\n[3/5] Initializing Sarvam services...")
try:
    # Load model configs from .env (same as Sarvam_v2)
    stt_model = os.getenv("STT_MODEL", "saaras:v3")
    stt_language = os.getenv("STT_LANGUAGE", "unknown")
    tts_model = os.getenv("TTS_MODEL", "bulbul:v3")
    tts_speaker = os.getenv("TTS_SPEAKER", "priya")
    tts_language = os.getenv("TTS_TARGET_LANGUAGE", "en-IN")
    llm_model = os.getenv("LLM_MODEL", "sarvam-30b")
    
    print(f"  STT: {stt_model} (lang: {stt_language})")
    print(f"  TTS: {tts_model} / {tts_speaker} (lang: {tts_language})")
    print(f"  LLM: {llm_model}")
    
    stt_settings = SarvamSTTService.Settings(
        model=stt_model,
        language=None if stt_language == "unknown" else stt_language,
    )
    stt = SarvamSTTService(api_key=sarvam_key, settings=stt_settings)
    print("✓ SarvamSTTService created")
    
    llm_settings = SarvamLLMService.Settings(model=llm_model)
    llm = SarvamLLMService(api_key=sarvam_key, settings=llm_settings)
    print("✓ SarvamLLMService created")
    
    tts_settings = SarvamTTSService.Settings(
        model=tts_model,
        voice=tts_speaker,
        language=tts_language,
    )
    tts = SarvamTTSService(api_key=sarvam_key, settings=tts_settings)
    print("✓ SarvamTTSService created")
except Exception as e:
    print(f"✗ Failed to create Sarvam services: {e}")
    sys.exit(1)

# Test 4: Initialize business logic
print("\n[4/5] Initializing business logic...")
try:
    backend = BackendClient(backend_url)
    print("✓ BackendClient created")
    
    booking_manager = VoiceBookingManager(backend)
    print("✓ VoiceBookingManager created")
    
    system_prompt = get_system_prompt("en")
    print(f"✓ System prompt loaded ({len(system_prompt)} chars)")
except Exception as e:
    print(f"✗ Failed to initialize business logic: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Test 5: Create processors
print("\n[5/5] Creating custom processors...")
try:
    audio_in = AudioInputProcessor()
    print("✓ AudioInputProcessor created")
    
    async def dummy_audio_callback(bytes_data):
        pass
    
    audio_out = AudioOutputProcessor(dummy_audio_callback)
    print("✓ AudioOutputProcessor created")
    
    booking_proc = BookingProcessor(
        session_id="test-session",
        clinic_id="clinic_001",
        phone="+919999999999",
        booking_manager=booking_manager,
    )
    print("✓ BookingProcessor created")
    
    assistant_capture = AssistantCaptureProcessor()
    print("✓ AssistantCaptureProcessor created")
except Exception as e:
    print(f"✗ Failed to create processors: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 60)
print("✓ All tests passed! Pipecat-Sarvam integration is ready.")
print("=" * 60)
print("\nNext steps:")
print("1. Start Backend_saas server (if not already running)")
print("2. Run: python server.py")
print("3. Open browser: http://localhost:19200")
print("=" * 60)
