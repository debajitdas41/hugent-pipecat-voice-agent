"""
Pipecat Custom Processors

This package contains custom frame processors for the Sarvam Voice Agent:
- AudioInputProcessor: WebSocket → AudioRawFrame
- AudioOutputProcessor: AudioRawFrame → WebSocket
- BookingProcessor: Business logic integration (extraction, booking state, context injection)
- AssistantCaptureProcessor: Barge-in detection
"""

from .audio_input import AudioInputProcessor
from .audio_output import AudioOutputProcessor
from .booking_processor import BookingProcessor
from .assistant_capture import AssistantCaptureProcessor

__all__ = [
    "AudioInputProcessor",
    "AudioOutputProcessor",
    "BookingProcessor",
    "AssistantCaptureProcessor",
]
