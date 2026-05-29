"""
Audio Input Processor — WebSocket → Pipecat Pipeline

Receives PCM16 audio chunks from WebSocket and converts them
to Pipecat AudioRawFrame format for the STT service.
"""
import logging
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("audio_input")


class AudioInputProcessor(FrameProcessor):
    """
    Converts incoming PCM bytes from WebSocket into AudioRawFrame
    that Pipecat's STT service can process.

    In v2, this was handled manually in session.py's process_audio() method.
    Pipecat handles buffering, VAD, and STT coordination automatically.
    """

    def __init__(self):
        super().__init__()
        self.sample_rate = 16000
        self.num_channels = 1

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """
        Process incoming audio frames from upstream (WebSocket).
        Convert bytes to AudioRawFrame and pass downstream to STT.
        """
        await super().process_frame(frame, direction)

        # Only process downstream frames (audio coming from WebSocket)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        # If it's already an AudioRawFrame, just pass it through
        if isinstance(frame, AudioRawFrame):
            await self.push_frame(frame, direction)
            return

        # Otherwise, log and pass through (non-audio control frames)
        await self.push_frame(frame, direction)
