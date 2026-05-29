"""
Sarvam AI Streaming TTS — Bulbul v3 WebSocket
Streams text → PCM16 audio chunks with low latency.
"""
import os
import asyncio
import base64
import logging
from typing import Callable, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("tts")

from sarvamai import AsyncSarvamAI, AudioOutput, EventResponse


class SarvamTTS:
    """
    Streaming TTS backed by Sarvam Bulbul v3 WebSocket.

    Usage:
      tts = SarvamTTS()
      await tts.stream("Hello, how are you?", on_pcm_chunk=callback)
    """

    def __init__(self):
        api_key = os.getenv("SARVAM_API_KEY")
        if not api_key:
            raise RuntimeError("SARVAM_API_KEY is not set")
        self._api_key = api_key
        self._client: Optional[AsyncSarvamAI] = None

        # Config
        self.model = os.getenv("TTS_MODEL", "bulbul:v3")
        self.speaker = os.getenv("TTS_SPEAKER", "priya")
        self.target_language = os.getenv("TTS_TARGET_LANGUAGE", "en-IN")
        self.sample_rate = int(os.getenv("TTS_SAMPLE_RATE", "16000"))

        logger.info(
            f"SarvamTTS init: model={self.model} speaker={self.speaker} "
            f"lang={self.target_language} sr={self.sample_rate}"
        )

    def _ensure_client(self) -> "AsyncSarvamAI":
        if self._client is None:
            self._client = AsyncSarvamAI(api_subscription_key=self._api_key)
        return self._client

    async def stream(
        self,
        text: str,
        on_pcm_chunk: Callable[[bytes], None],
        language: str | None = None,
    ):
        """
        Stream TTS for `text`, calling `on_pcm_chunk(bytes)` for each
        raw PCM16 audio chunk received (16-bit signed LE, mono).
        If `language` is provided, it overrides the default target_language.
        """
        if not text or not text.strip():
            logger.warning("TTS stream called with empty text")
            return

        target_lang = language or self.target_language
        client = self._ensure_client()
        chunk_count = 0

        try:
            async with client.text_to_speech_streaming.connect(
                model=self.model,
                send_completion_event=True,
            ) as ws:
                # Send configuration — linear16 = raw PCM16 output
                await ws.configure(
                    target_language_code=target_lang,
                    speaker=self.speaker,
                    output_audio_codec="linear16",
                    speech_sample_rate=self.sample_rate,
                )

                # Send text
                await ws.convert(text)
                await ws.flush()

                # Receive audio chunks
                async for msg in ws:
                    if isinstance(msg, AudioOutput):
                        audio_b64 = msg.data.audio
                        pcm_bytes = base64.b64decode(audio_b64)
                        chunk_count += 1
                        await on_pcm_chunk(pcm_bytes)
                    elif isinstance(msg, EventResponse):
                        if hasattr(msg, "data") and hasattr(msg.data, "event_type"):
                            if msg.data.event_type == "final":
                                break
                    elif isinstance(msg, dict):
                        # Fallback dict handling
                        if "audio" in msg.get("data", {}):
                            audio_b64 = msg["data"]["audio"]
                            pcm_bytes = base64.b64decode(audio_b64)
                            chunk_count += 1
                            await on_pcm_chunk(pcm_bytes)
                        elif msg.get("data", {}).get("event_type") == "final":
                            break

        except Exception as e:
            logger.error(f"TTS stream error: {e}", exc_info=True)

        logger.debug(f"TTS streamed {chunk_count} chunks for: '{text[:50]}...'")

    async def stream_sentences(
        self,
        sentence_gen,
        on_pcm_chunk: Callable[[bytes], None],
        language: str | None = None,
    ):
        """
        Low-latency TTS pipeline: keep one WebSocket open and feed sentences as
        they arrive from the LLM stream.  Audio starts playing as soon as the
        first sentence is converted — no need to wait for the full response.

        sentence_gen: async generator yielding complete sentence strings.
        """
        target_lang = language or self.target_language
        client = self._ensure_client()

        try:
            async with client.text_to_speech_streaming.connect(
                model=self.model,
                send_completion_event=True,
            ) as ws:
                await ws.configure(
                    target_language_code=target_lang,
                    speaker=self.speaker,
                    output_audio_codec="linear16",
                    speech_sample_rate=self.sample_rate,
                )

                # Producer runs concurrently: feeds sentences into the WS
                produce_task = asyncio.create_task(
                    self._produce_sentences(ws, sentence_gen)
                )

                try:
                    async for msg in ws:
                        if isinstance(msg, AudioOutput):
                            pcm_bytes = base64.b64decode(msg.data.audio)
                            await on_pcm_chunk(pcm_bytes)
                        elif isinstance(msg, EventResponse):
                            if hasattr(msg, "data") and hasattr(msg.data, "event_type"):
                                if msg.data.event_type == "final":
                                    break
                        elif isinstance(msg, dict):
                            if msg.get("data", {}).get("event_type") == "final":
                                break
                finally:
                    if not produce_task.done():
                        produce_task.cancel()
                        try:
                            await produce_task
                        except asyncio.CancelledError:
                            pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"TTS stream_sentences error: {e}", exc_info=True)

    @staticmethod
    async def _produce_sentences(ws, sentence_gen):
        """Feed sentences to TTS WebSocket as they arrive from the LLM."""
        try:
            async for sentence in sentence_gen:
                if sentence.strip():
                    await ws.convert(sentence)
            await ws.flush()
        except asyncio.CancelledError:
            try:
                await ws.flush()
            except Exception:
                pass
            raise
        except Exception as e:
            logger.error(f"TTS produce_sentences error: {e}")
            try:
                await ws.flush()
            except Exception:
                pass

    async def synthesize(self, text: str) -> bytes:
        """
        Non-streaming TTS — returns complete PCM16 audio as bytes.
        Useful for short messages or fallback.
        """
        if not text or not text.strip():
            return b""

        chunks: list[bytes] = []

        async def collect(pcm: bytes):
            chunks.append(pcm)

        await self.stream(text, on_pcm_chunk=collect)
        return b"".join(chunks)

    async def close(self):
        self._client = None
        logger.info("SarvamTTS closed")
