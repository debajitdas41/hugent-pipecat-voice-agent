"""
Assistant Capture Processor — Barge-in Detection

Monitors LLM response start/end to detect when the assistant is speaking.
Used to implement barge-in (interrupt assistant with new user speech).
"""
import logging
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("assistant_capture")


class AssistantCaptureProcessor(FrameProcessor):
    """
    Tracks assistant speaking state for barge-in support.

    In v2, this was handled manually with SessionState enum (SPEAKING, INTERRUPTED).
    Pipecat provides frame events that signal when TTS starts/stops.

    When barge-in is enabled, Pipecat automatically cancels TTS on new user speech.
    This processor logs the events for debugging and metrics.
    """

    def __init__(self):
        super().__init__()
        self.assistant_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """
        Monitor LLM and TTS frames to track assistant speaking state.
        """
        await super().process_frame(frame, direction)

        # Detect TTS start (assistant starts speaking)
        if isinstance(frame, (TTSStartedFrame, LLMFullResponseStartFrame)):
            self.assistant_speaking = True
            logger.debug("Assistant started speaking")

        # Detect TTS end (assistant finished speaking)
        elif isinstance(frame, (TTSStoppedFrame, LLMFullResponseEndFrame)):
            self.assistant_speaking = False
            logger.debug("Assistant stopped speaking")

        # Pass frame downstream
        await self.push_frame(frame, direction)
