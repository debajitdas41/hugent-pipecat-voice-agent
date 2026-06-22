"""
Pipecat Pipeline Worker — Replaces session.py's manual state management

This file builds a Pipecat pipeline with:
- Audio input from WebSocket
- Sarvam STT → Booking Processor → Sarvam LLM → Sarvam TTS
- Audio output to WebSocket
- Barge-in support via AssistantCaptureProcessor

Pipecat handles all state management, turn-taking, and frame routing automatically.
"""
import os
import asyncio
import logging
from typing import Callable, Optional, Awaitable

# Pipecat framework
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
# Import Sarvam services from their specific modules (not from __init__)
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
# Custom LLM processor wrapping raw HTTP calls to Sarvam Chat Completions API
from processors.sarvam_llm import SarvamLLMProcessor
from pipecat.frames.frames import (
    Frame,
    AudioRawFrame,
    InputAudioRawFrame,
    LLMMessagesAppendFrame,
    EndFrame,
)
# Context aggregators — required for LLM to receive user messages
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

# Business logic imports
from services.backend_client import BackendClient
from booking.manager import VoiceBookingManager
from prompts.loader import get_system_prompt, get_smart_prompt

# Custom processors (we'll create these next)
from processors.audio_output import AudioOutputProcessor
from processors.booking_processor import BookingProcessor
from processors.assistant_capture import AssistantCaptureProcessor

logger = logging.getLogger("pipeline")


class PipecatWorker:
    """
    Manages a Pipecat pipeline for one voice session.
    Replaces SessionWorker from v2 — uses Pipecat's frame-based architecture
    instead of manual state machine.
    """

    def __init__(
        self,
        session_id: str,
        clinic_id: str,
        phone: str,
        on_audio_chunk: Callable[[bytes], Awaitable[None]],
        on_control_message: Callable[[dict], Awaitable[None]],
        on_barge_in: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.session_id = session_id
        self.clinic_id = clinic_id
        self.phone = phone
        self.on_audio_chunk = on_audio_chunk
        self.on_control_message = on_control_message

        self.on_barge_in = on_barge_in

        self.pipeline: Optional[Pipeline] = None
        self.task: Optional[PipelineTask] = None
        self.runner: Optional[PipelineRunner] = None
        self.pipeline_task: Optional[asyncio.Task] = None  # Background task running the pipeline
        self._booking_processor: Optional["BookingProcessor"] = None  # kept for delayed greeting

        self.language = "en"  # Start with English, switch on Turn 2
        self.turn_count = 0

        # Business logic instances (same as v2)
        backend_url = os.getenv("BACKEND_API_BASE_URL", "http://localhost:8000")
        self.backend = BackendClient(backend_url)
        self.booking_manager = VoiceBookingManager(self.backend)

        logger.info(f"[{self.session_id}] PipecatWorker created for {clinic_id}/{phone}")

    async def start(self):
        """
        Build and start the Pipecat pipeline.
        This replaces session.py's manual initialization.
        """
        try:
            # ── 1. Create Sarvam services ──
            sarvam_api_key = os.getenv("SARVAM_API_KEY")
            if not sarvam_api_key:
                raise ValueError("SARVAM_API_KEY not set in environment")

            # Load model configs from .env (same as Sarvam_v2)
            stt_model = os.getenv("STT_MODEL", "saaras:v3")
            stt_language = os.getenv("STT_LANGUAGE", "unknown")
            tts_model = os.getenv("TTS_MODEL", "bulbul:v3")
            tts_speaker = os.getenv("TTS_SPEAKER", "priya")
            tts_language = os.getenv("TTS_TARGET_LANGUAGE", "unknown")
            llm_model = os.getenv("LLM_MODEL", "sarvam-30b")  # read by SarvamLLMProcessor via llm.py

            # Prefetch clinic info once — avoids per-turn API calls in BookingProcessor
            clinic_info = await self._fetch_clinic_info()

            # STT: Sarvam Saaras STT service
            # vad_signals=True emits VADUserStartedSpeakingFrame/VADUserStoppedSpeakingFrame
            # which are required by SpeechTimeoutUserTurnStopStrategy
            stt_settings = SarvamSTTService.Settings(
                model=stt_model,
                language=None if stt_language == "unknown" else stt_language,
                vad_signals=True,
                high_vad_sensitivity=False,  # True is too sensitive for calls with background noise/voices
            )
            stt = SarvamSTTService(
                api_key=sarvam_api_key,
                settings=stt_settings,
            )

            # LLM: custom processor — raw HTTP to Sarvam Chat Completions API
            llm = SarvamLLMProcessor(on_control_message=self.on_control_message)
            logger.info(f"[{self.session_id}] Using SarvamLLMProcessor with model={llm_model}")

            # TTS: Sarvam Bulbul TTS service
            # Start with en-IN (Bulbul v3 is multilingual — it handles Hindi/Tamil/etc.
            # scripts correctly even with en-IN target). BookingProcessor sends
            # TTSUpdateSettingsFrame to switch to the user's chosen language after
            # language selection completes.
            tts_settings = SarvamTTSService.Settings(
                model=tts_model,
                voice=tts_speaker,
                language="en-IN",
                pace=1.05,
            )
            tts = SarvamTTSService(
                api_key=sarvam_api_key,
                sample_rate=8000,   # Must match FreJun telephony output (8kHz PCM)
                settings=tts_settings,
                min_buffer_size=25,
            )

            # ── 2. Create LLM context with system prompt ──
            # Use universal smart prompt (language-agnostic — sarvam-30b adapts naturally)
            clinic_name = clinic_info.get("name", "") if clinic_info else ""
            depts = clinic_info.get("departments", []) if clinic_info else []
            hours = clinic_info.get("working_hours", {}) if clinic_info else {}
            if isinstance(hours, dict):
                hours = ", ".join(f"{k}: {v}" for k, v in hours.items())
            system_prompt = get_smart_prompt(
                clinic_name=clinic_name or None,
                departments=depts or None,
                working_hours=hours or None,
                caller_phone=self.phone,
            )
            context = LLMContext(messages=[{"role": "system", "content": system_prompt}])

            # ── 3. Create context aggregator pair ──
            # context_pair.user(): TranscriptionFrame → LLMContextFrame → LLM
            # context_pair.assistant(): captures LLM response into context history
            # CRITICAL: assistant() must be at the END of the pipeline (after TTS+audio)
            # SpeechTimeoutUserTurnStopStrategy: fires 0.5s after VAD detects silence
            # (vs default ONNX SmartTurn which takes 1.2s+)
            turn_debounce = float(os.getenv("TURN_DEBOUNCE_SECS", "0.5"))
            context_pair = LLMContextAggregatorPair(
                context,
                user_params=LLMUserAggregatorParams(
                    user_turn_strategies=UserTurnStrategies(
                        stop=[SpeechTimeoutUserTurnStopStrategy(
                            user_speech_timeout=turn_debounce,
                        )],
                    ),
                ),
            )

            # ── 4. Create custom processors ──
            audio_output = AudioOutputProcessor(self.on_audio_chunk)
            booking_processor = BookingProcessor(
                session_id=self.session_id,
                clinic_id=self.clinic_id,
                phone=self.phone,
                booking_manager=self.booking_manager,
                llm_context=context,
                clinic_info=clinic_info,
                on_control_message=self.on_control_message,
                on_audio_chunk=self.on_audio_chunk,
                sarvam_api_key=sarvam_api_key,
                tts_model=tts_model,
                tts_speaker=tts_speaker,
                on_barge_in=self.on_barge_in,
            )
            self._booking_processor = booking_processor
            assistant_capture = AssistantCaptureProcessor()

            # ── 5. Build pipeline (frame flow) ──
            # STT → Booking → user_agg → LLM → TTS → audio_out → assistant_agg
            # NOTE: assistant() is at the END so TextFrame flows freely to TTS
            self.pipeline = Pipeline([
                stt,                       # Speech → TranscriptionFrame (with VAD)
                booking_processor,         # Inject booking context
                context_pair.user(),       # TranscriptionFrame → LLMContextFrame → LLM
                llm,                       # LLMContextFrame → TextFrame (streaming)
                assistant_capture,         # Monitor TTS start/end for barge-in
                tts,                       # TextFrame → OutputAudioRawFrame
                audio_output,              # OutputAudioRawFrame → WebSocket PCM
                context_pair.assistant(),  # Captures assistant text into context history
            ])

            # ── 6. Create pipeline task ──
            # idle_timeout_secs: how long with no frames in the task queue before
            # auto-cancel. Push_frame() from inside processors does NOT reset this
            # timer — only task.queue_frame() does (i.e. incoming user audio frames).
            # Set generously so slow backend reservation calls don’t trigger it.
            pipeline_idle_timeout = float(os.getenv("PIPELINE_IDLE_TIMEOUT_SECS", "300"))
            self.task = PipelineTask(
                self.pipeline,
                params=PipelineParams(
                    allow_interruptions=True,
                    enable_metrics=False,
                    enable_usage_metrics=False,
                ),
                idle_timeout_secs=pipeline_idle_timeout,
                enable_turn_tracking=False,
                enable_rtvi=False,
            )

            # ── 7. Create pipeline runner and start in background ──
            self.runner = PipelineRunner(handle_sigint=False)  # We handle shutdown ourselves in cleanup()
            self.pipeline_task = asyncio.create_task(self.runner.run(self.task))

            # ── 8. Proactive greeting — speak before user says anything ──
            # Short delay lets the Sarvam STT WebSocket connect before audio is sent.
            asyncio.create_task(self._delayed_greeting(delay=1.0))

            logger.info(f"[{self.session_id}] Pipecat pipeline started in background")

        except Exception as e:
            logger.error(f"[{self.session_id}] Failed to start pipeline: {e}", exc_info=True)
            raise

    async def process_audio(self, pcm_chunk: bytes):
        """
        Push incoming audio from WebSocket into the pipeline.
        Pipecat handles buffering, VAD, and STT automatically.
        """
        if not self.task:
            return

        # Use InputAudioRawFrame — this is what STT services expect
        frame = InputAudioRawFrame(
            audio=pcm_chunk,
            sample_rate=16000,
            num_channels=1,
        )

        # Queue frame into pipeline (non-blocking)
        await self.task.queue_frame(frame)

    async def _delayed_greeting(self, delay: float = 1.0):
        """Wait briefly for STT to connect, then trigger the proactive greeting."""
        try:
            await asyncio.sleep(delay)
            if self._booking_processor is not None:
                await self._booking_processor.trigger_greeting()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.session_id}] Delayed greeting error: {e}")

    async def _fetch_clinic_info(self) -> dict:
        """Fetch clinic info once at session start to avoid per-turn API calls."""
        try:
            clinic = await self.booking_manager.backend.get_clinic(self.clinic_id)
            info = clinic or {}
            logger.info(f"[{self.session_id}] Clinic info prefetched: {info.get('name', '(unknown)')}")
            return info
        except Exception as e:
            logger.warning(f"[{self.session_id}] Failed to prefetch clinic info: {e}")
            return {}

    def on_browser_playback_complete(self):
        """
        Browser finished playing TTS audio.
        In Pipecat, this is handled automatically via frame events,
        but we keep this for compatibility with v2 client protocol.
        """
        # Pipecat handles playback state internally via EndFrame and metrics
        pass

    async def cleanup(self):
        """
        Stop the pipeline and cleanup resources.
        """
        try:
            # Cancel any pending booking processor transcript tasks
            if self._booking_processor is not None:
                self._booking_processor.cancel_pending_tasks()

            if self.task:
                # Cancel the pipeline task gracefully
                await self.task.cancel()

            if self.pipeline_task and not self.pipeline_task.done():
                # Cancel the background runner task
                self.pipeline_task.cancel()
                try:
                    await self.pipeline_task
                except asyncio.CancelledError:
                    pass

            if self.backend:
                await self.backend.close()

            logger.info(f"[{self.session_id}] Pipeline cleaned up")

        except Exception as e:
            logger.error(f"[{self.session_id}] Cleanup error: {e}")
