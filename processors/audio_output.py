"""
Audio Output Processor — Pipecat Pipeline → WebSocket

Receives AudioRawFrame from TTS and sends PCM bytes back to
the WebSocket client for playback in the browser.
"""
import logging
from typing import Callable, Awaitable
from pipecat.frames.frames import OutputAudioRawFrame, Frame, TTSAudioRawFrame, TTSStoppedFrame
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

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # OutputAudioRawFrame is the terminal audio frame from TTS (TTSAudioRawFrame extends it)
        # No direction check needed — audio always flows downstream here
        logger.info(f"AudioOutputProcessor received frame: {type(frame).__name__}, direction: {direction.name}")
        if isinstance(frame, TTSAudioRawFrame):
            try:
                await self.on_audio_chunk(bytes(frame.audio))
            except Exception as e:
                logger.error(f"Error sending audio to WebSocket: {e}")
        elif isinstance(frame, TTSStoppedFrame):
            try:
                await self.on_audio_chunk(end_of_tts=True)
                logger.info(f"Sent end-of-TTS audio chunk to WebSocket")
            except Exception as e:
                logger.error(f"Error sending audio to WebSocket: {e}")
        else:
            # Pass non-audio frames downstream (EndFrame, metrics, etc.)
            await self.push_frame(frame, direction)
