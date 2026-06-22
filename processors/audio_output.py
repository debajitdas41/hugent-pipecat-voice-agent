"""
Audio Output Processor — Pipecat Pipeline → WebSocket

Receives AudioRawFrame from TTS and sends PCM bytes back to
the WebSocket client for playback in the browser.
"""
import logging
from typing import Callable, Awaitable
from pipecat.frames.frames import OutputAudioRawFrame, Frame, TTSAudioRawFrame, TTSStoppedFrame, LLMFullResponseEndFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("audio_output")


class AudioOutputProcessor(FrameProcessor):
    """
    Converts AudioRawFrame from TTS into PCM bytes and sends
    them to the WebSocket callback.

    In v2, this was handled manually in session.py's TTS response handler.
    Pipecat automatically streams TTS output as AudioRawFrame chunks.
    """

    def __init__(self, on_audio_chunk: Callable[[bytes, bool], Awaitable[None]]):
        super().__init__()
        self.on_audio_chunk = on_audio_chunk
        self._tts_chunk_count = 0
        self._tts_total_bytes = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            try:
                chunk = bytes(frame.audio)
                self._tts_chunk_count += 1
                self._tts_total_bytes += len(chunk)
                logger.debug(
                    f"[audio_out] TTS chunk #{self._tts_chunk_count}: "
                    f"{len(chunk)}B (total so far: {self._tts_total_bytes}B)"
                )
                await self.on_audio_chunk(chunk)
            except Exception as e:
                logger.error(f"Error sending audio to WebSocket: {e}")
        elif isinstance(frame, TTSStoppedFrame):
            # Do NOT flush here — more TTS sentences may follow in the same LLM turn.
            # Flushing per-sentence injects up to 600ms of silence padding between each
            # sentence, causing the mid-sentence "breaks". Pass through so downstream
            # processors (context tracking) still see the frame.
            logger.debug("[audio_out] TTSStoppedFrame — holding buffer for next sentence")
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Full LLM response is done — now flush the tail of the pacing buffer
            # (one flush per turn instead of one per sentence).
            try:
                logger.info(
                    f"[audio_out] LLMFullResponseEndFrame — flushing tail. "
                    f"Total: {self._tts_chunk_count} chunks, {self._tts_total_bytes}B"
                )
                self._tts_chunk_count = 0
                self._tts_total_bytes = 0
                await self.on_audio_chunk(b"", end_of_tts=True)
            except Exception as e:
                logger.error(f"Error sending end-of-TTS signal: {e}")
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)
