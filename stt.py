"""
Sarvam AI Streaming STT — Saaras v3 WebSocket
Real-time speech-to-text with built-in VAD (no custom VAD needed).
Emits speech_start / speech_end / transcript events.
"""
import os
import asyncio
import base64
import json
import logging
from typing import Callable, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("stt")

from sarvamai import AsyncSarvamAI, SarvamAI


class SarvamSTT:
    """
    Streaming STT backed by Sarvam Saaras v3 WebSocket.

    Usage (two modes):
      A) **One-shot** — collect PCM buffer, call `transcribe_pcm()`
      B) **Streaming** — call `start_stream()`, feed chunks via `send_audio()`,
         receive events via `on_event` callback, call `stop_stream()`

    The streaming mode exposes Sarvam's built-in VAD which fires
    `speech_start` / `speech_end` / `transcript` events so the
    session layer does not need a custom VAD.
    """

    def __init__(self):
        api_key = os.getenv("SARVAM_API_KEY")
        if not api_key:
            raise RuntimeError("SARVAM_API_KEY is not set")
        self._api_key = api_key
        self._client: Optional[AsyncSarvamAI] = None

        # Config (from env)
        self.sample_rate = int(os.getenv("STT_SAMPLE_RATE", "16000"))
        self.language = os.getenv("STT_LANGUAGE", "unknown")
        self.model = os.getenv("STT_MODEL", "saaras:v3")

        # Streaming state
        self._ws = None
        self._ws_ctx = None  # async context manager from connect()
        self._recv_task: Optional[asyncio.Task] = None
        self._on_event: Optional[Callable] = None  # async callback(event_dict)
        self._streaming = False

        logger.info(
            f"SarvamSTT init: model={self.model} lang={self.language} sr={self.sample_rate}"
        )

    # ------------------------------------------------------------------
    #  Internals
    # ------------------------------------------------------------------
    def _ensure_client(self) -> "AsyncSarvamAI":
        if self._client is None:
            self._client = AsyncSarvamAI(api_subscription_key=self._api_key)
        return self._client

    # ------------------------------------------------------------------
    #  Mode A: One-shot transcription (for compatibility / fallback)
    # ------------------------------------------------------------------
    async def transcribe_pcm(self, pcm_audio: bytes) -> tuple[str, float]:
        """
        Transcribe a complete PCM16 audio buffer.
        Returns (transcript, confidence).
        """
        client = self._ensure_client()
        audio_b64 = base64.b64encode(pcm_audio).decode("utf-8")

        transcript = ""
        confidence = 0.0

        try:
            async with client.speech_to_text_streaming.connect(
                model=self.model,
                mode="transcribe",
                language_code=self.language,
                high_vad_sensitivity=True,
                flush_signal=True,
                input_audio_codec="pcm_s16le",
                sample_rate=str(self.sample_rate),
            ) as ws:
                await ws.transcribe(
                    audio=audio_b64,
                    encoding="audio/wav",
                    sample_rate=self.sample_rate,
                )
                await ws.flush()

                async for msg in ws:
                    # SDK may return objects or dicts depending on version
                    if isinstance(msg, dict):
                        mtype = msg.get("type", "")
                        if mtype == "transcript" or "transcript" in msg:
                            transcript = msg.get("transcript", msg.get("text", ""))
                            confidence = 0.95
                            break
                    else:
                        # Object-style response from SDK
                        if hasattr(msg, "data"):
                            data = msg.data
                            if hasattr(data, "transcript"):
                                transcript = data.transcript or ""
                                confidence = 0.95
                                break
                        elif hasattr(msg, "transcript"):
                            transcript = msg.transcript or ""
                            confidence = 0.95
                            break

        except Exception as e:
            logger.error(f"STT one-shot error: {e}")

        transcript = transcript.strip()
        logger.info(f"STT one-shot: '{transcript}' (conf={confidence:.2f})")
        return transcript, confidence

    # ------------------------------------------------------------------
    #  Mode B: Persistent streaming (used by SessionWorker)
    # ------------------------------------------------------------------
    async def start_stream(self, on_event: Callable):
        """
        Open a persistent STT WebSocket connection.
        `on_event` is an async callback receiving dicts:
          {"type": "speech_start"}
          {"type": "speech_end"}
          {"type": "transcript", "text": "..."}
        """
        if self._streaming:
            logger.warning("Stream already active — ignoring start_stream()")
            return

        self._on_event = on_event
        client = self._ensure_client()

        self._ws_ctx = client.speech_to_text_streaming.connect(
            model=self.model,
            mode="transcribe",
            language_code=self.language,
            high_vad_sensitivity=True,
            vad_signals=True,
            flush_signal=True,
            input_audio_codec="pcm_s16le",
            sample_rate=str(self.sample_rate),
        )
        self._ws = await self._ws_ctx.__aenter__()

        self._streaming = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("STT stream started")

    async def send_audio(self, pcm_chunk: bytes):
        """Send a raw PCM16 chunk to the open STT stream."""
        if not self._streaming or self._ws is None:
            return
        try:
            audio_b64 = base64.b64encode(pcm_chunk).decode("utf-8")
            await self._ws.transcribe(
                audio=audio_b64,
                encoding="audio/wav",
                sample_rate=self.sample_rate,
            )
        except Exception as e:
            logger.error(f"STT send_audio error: {e}")

    async def flush(self):
        """Force-flush the STT buffer to get a transcript now."""
        if not self._streaming or self._ws is None:
            return
        try:
            await self._ws.flush()
        except Exception as e:
            logger.error(f"STT flush error: {e}")

    async def stop_stream(self):
        """Close the persistent STT stream."""
        self._streaming = False
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        if self._ws_ctx is not None:
            try:
                await self._ws_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._ws_ctx = None
            self._ws = None
        logger.info("STT stream stopped")

    async def _recv_loop(self):
        """Background task reading events from the STT WebSocket."""
        try:
            async for msg in self._ws:
                event = self._parse_event(msg)
                if event and self._on_event:
                    await self._on_event(event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"STT recv_loop error: {e}")
            # Signal a disconnect so session can handle reconnecting
            if self._on_event:
                await self._on_event({"type": "error", "error": str(e)})

    @staticmethod
    def _parse_event(msg) -> Optional[dict]:
        """Normalize raw SDK message into a simple dict."""
        if isinstance(msg, dict):
            mtype = msg.get("type", "")
            if mtype == "speech_start":
                return {"type": "speech_start"}
            if mtype == "speech_end":
                return {"type": "speech_end"}
            if mtype in ("transcript", "data"):
                text = msg.get("transcript", msg.get("text", ""))
                if text:
                    return {"type": "transcript", "text": text.strip()}
            return None

        # Object-style SDK response
        if hasattr(msg, "type"):
            mtype = getattr(msg, "type", "")
        elif hasattr(msg, "data") and hasattr(msg.data, "type"):
            mtype = msg.data.type
        else:
            mtype = ""

        if mtype == "speech_start":
            return {"type": "speech_start"}
        if mtype == "speech_end":
            return {"type": "speech_end"}

        # Try to extract transcript
        text = ""
        if hasattr(msg, "data"):
            data = msg.data
            text = getattr(data, "transcript", "") or getattr(data, "text", "") or ""
            if not text and hasattr(data, "request_id"):
                text = getattr(data, "transcript", "")
        elif hasattr(msg, "transcript"):
            text = msg.transcript or ""

        if text:
            return {"type": "transcript", "text": text.strip()}

        return None

    async def close(self):
        """Cleanup everything."""
        await self.stop_stream()
        self._client = None
        logger.info("SarvamSTT closed")
