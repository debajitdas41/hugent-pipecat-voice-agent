"""
Sarvam AI Voice Agent — Pipecat Implementation
FastAPI server with WebSocket streaming + Pipecat pipeline
"""
import base64
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from tokenize import String
from typing import Dict
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

load_dotenv(override=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="[%(name)s %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

# Suppress noisy Pipecat internal frame-routing logs (keep our own loggers at INFO)
logging.getLogger("pipecat").setLevel(logging.WARNING)

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "19200"))
DEFAULT_CLINIC_ID = os.getenv("DEFAULT_CLINIC_ID", "clinic_001")
DEFAULT_PHONE = os.getenv("DEFAULT_PHONE", "+919999999999")
SESSIONS = {} 
TELECOM_SAMPLE_RATE = 8000
PIPELINE_SAMPLE_RATE = 16000

app = FastAPI(title="Sarvam AI Voice Agent - Pipecat")

from pipeline_worker import PipecatWorker

# Store active pipeline workers (replaces sessions dict from v2)
workers: dict[str, PipecatWorker] = {}


class FreJunAudioBuffer:
    """
    FIFO pacing buffer for FreJun telephony compatibility.

    Fixes applied (per FreJun's audio quality diagnosis):
      Fix 1 — Byte alignment: odd-byte carry prevents sample-offset bursts.
      Fix 2 — Fade in/out: 5ms linear taper at each chunk boundary kills pops.
      Fix 3 — IIR low-pass filter: single-pole filter (α=0.85) removes TTS
               quantisation spikes before they enter the buffer.
    """

    # Fade window: 40 samples = 5ms @ 8kHz
    _FADE_SAMPLES = 40
    # LPF coefficient: α=0.97 → cutoff ≈ 3700Hz @ 8kHz.
    # Kills single-sample quantisation spikes (>4kHz) while preserving all
    # speech content (300–3400Hz) with <1dB roll-off.
    # Previous α=0.85 had cutoff at ~1080Hz → -10.5dB at 3400Hz (muffled voice).
    _LPF_ALPHA = 0.97

    def __init__(self, sample_rate: int = 8000, target_ms: int = 500):
        self.sample_rate = sample_rate
        self.target_ms = target_ms
        self.bytes_per_sample = 2  # 16-bit PCM
        self.target_bytes = (sample_rate * target_ms // 1000) * self.bytes_per_sample
        self.buffer = bytearray()
        # Fix 1: carry odd byte across calls
        self._odd_carry: bytes = b""
        # Fix 3: LPF state (persists across chunks for continuity)
        self._lpf_state: float = 0.0
        # Fix 2: fade only at TTS segment boundaries (not every 600ms pacing chunk).
        # True after flush() or clear() — first chunk of a new TTS segment gets
        # a fade-in. All mid-segment chunks get no fade (no pulsing artefact).
        self._first_chunk_after_flush: bool = True

        logger.debug(
            f"FreJunAudioBuffer initialized: {sample_rate}Hz, "
            f"target={target_ms}ms ({self.target_bytes} bytes)"
        )

    def add_chunk(self, pcm_bytes: bytes) -> list[bytes]:
        # ── Fix 1: byte alignment ───────────────────────────────────────────
        # Prepend any carried byte from the previous call, then strip any new
        # odd tail and carry it forward. This keeps the buffer sample-aligned.
        if self._odd_carry:
            pcm_bytes = self._odd_carry + pcm_bytes
            self._odd_carry = b""
        if len(pcm_bytes) % 2:
            self._odd_carry = pcm_bytes[-1:]
            pcm_bytes = pcm_bytes[:-1]
        if not pcm_bytes:
            return []

        # ── Fix 3: LPF before buffering (continuous state across calls) ─────
        pcm_bytes = self._apply_lpf(pcm_bytes)

        self.buffer.extend(pcm_bytes)
        ready_chunks = []

        while len(self.buffer) >= self.target_bytes:
            cut = self._nearest_zero_crossing(self.target_bytes)
            # Ensure cut is always even (sample-aligned)
            cut = cut & ~1
            chunk = bytes(self.buffer[:cut])
            del self.buffer[:cut]
            # ── Fix 2: fade only at TTS segment start (not every pacing chunk) ──
            # Fade-in on first chunk after a flush (= new TTS segment start).
            # No fade-out on mid-segment chunks — avoids pulsing every 600ms.
            if self._first_chunk_after_flush:
                chunk = self._apply_fade(chunk, fade_in=True, fade_out=False)
                self._first_chunk_after_flush = False
            ready_chunks.append(chunk)
            logger.debug(
                f"Emitting chunk: {len(chunk)}B "
                f"(zero-cross offset={cut - self.target_bytes:+d})"
            )

        return ready_chunks

    # ── Fix 3: single-pole IIR low-pass filter ─────────────────────────────

    def _apply_lpf(self, pcm_bytes: bytes) -> bytes:
        """
        y[n] = α·x[n] + (1−α)·y[n−1]
        Removes single-sample spikes while preserving speech (300–3400 Hz).
        """
        alpha = self._LPF_ALPHA
        inv_alpha = 1.0 - alpha
        y = self._lpf_state
        out = bytearray(len(pcm_bytes))
        for i in range(0, len(pcm_bytes), 2):
            raw = pcm_bytes[i] | (pcm_bytes[i + 1] << 8)
            x = raw - 65536 if raw >= 32768 else raw
            y = alpha * x + inv_alpha * y
            s = max(-32768, min(32767, int(y)))
            out[i] = s & 0xFF
            out[i + 1] = (s >> 8) & 0xFF
        self._lpf_state = y
        return bytes(out)

    # ── Fix 2: linear fade in/out ──────────────────────────────────────────

    def _apply_fade(self, chunk: bytes, fade_in: bool, fade_out: bool) -> bytes:
        """
        Linear amplitude taper over _FADE_SAMPLES at start and/or end.
        Eliminates the amplitude discontinuity at chunk boundaries → no pop.
        """
        n_samples = len(chunk) // 2
        fade = self._FADE_SAMPLES
        if n_samples < fade * 2:
            return chunk  # chunk too small to fade safely
        out = bytearray(chunk)
        if fade_in:
            for i in range(fade):
                scale = i / fade
                pos = i * 2
                raw = out[pos] | (out[pos + 1] << 8)
                s = raw - 65536 if raw >= 32768 else raw
                s = max(-32768, min(32767, int(s * scale)))
                out[pos] = s & 0xFF
                out[pos + 1] = (s >> 8) & 0xFF
        if fade_out:
            for i in range(fade):
                scale = (fade - 1 - i) / fade
                pos = (n_samples - fade + i) * 2
                raw = out[pos] | (out[pos + 1] << 8)
                s = raw - 65536 if raw >= 32768 else raw
                s = max(-32768, min(32767, int(s * scale)))
                out[pos] = s & 0xFF
                out[pos + 1] = (s >> 8) & 0xFF
        return bytes(out)

    def _nearest_zero_crossing(self, target: int) -> int:
        """
        Find the nearest zero-crossing to `target` within a ±256-byte window.
        Returns a sample-aligned index. Falls back to `target` if none found.
        """
        buf = self.buffer
        lo = max(2, target - 256) & ~1
        hi = min(len(buf) - 2, target + 256) & ~1

        best_pos = target & ~1
        best_dist = abs(best_pos - target)

        i = lo
        while i < hi:
            s0 = (buf[i + 1] << 8 | buf[i]) - (65536 if buf[i + 1] & 0x80 else 0)
            s1 = (buf[i + 3] << 8 | buf[i + 2]) - (65536 if buf[i + 3] & 0x80 else 0)
            if (s0 >= 0) != (s1 >= 0):
                dist = abs(i + 2 - target)
                if dist < best_dist:
                    best_dist = dist
                    best_pos = i + 2
            i += 2

        return best_pos

    def flush(self) -> bytes:
        """Return remaining buffered bytes padded to target_bytes (end of TTS segment).

        FreJun drops or mangles chunks shorter than the negotiated chunk_size.
        Padding with silence ensures the last words always play fully.
        """
        if self.buffer:
            # Ensure even length (Fix 1)
            if len(self.buffer) % 2:
                logger.debug("[buf] flush: dropping 1 odd byte from tail")
                self.buffer = self.buffer[:-1]
            if not self.buffer:
                logger.debug("[buf] flush: buffer empty after alignment, nothing to flush")
                return b""
            tail = bytes(self.buffer)
            # Pad with silence (zeros) to target_bytes so FreJun plays the full chunk
            if len(tail) < self.target_bytes:
                padding = self.target_bytes - len(tail)
                tail = tail + bytes(padding)
                logger.debug(
                    f"[buf] flush: padded {len(tail) - padding}B audio + {padding}B silence"
                )
            result = self._apply_fade(tail, fade_in=False, fade_out=True)
            remaining_ms = len(result) / self.target_bytes * self.target_ms
            logger.info(
                f"[buf] flush: {len(result)}B (~{remaining_ms:.0f}ms) — TTS segment end"
            )
            self.buffer.clear()
            # Next TTS segment starts fresh → fade-in its first chunk
            self._first_chunk_after_flush = True
            return result
        logger.debug("[buf] flush: buffer already empty")
        return b""

    def clear(self) -> None:
        """Discard all buffered bytes without sending (barge-in path)."""
        if self.buffer:
            logger.info(
                f"[buf] clear (barge-in): discarding {len(self.buffer)}B + "
                f"carry={len(self._odd_carry)}B"
            )
            self.buffer.clear()
        self._odd_carry = b""
        self._lpf_state = 0.0
        self._first_chunk_after_flush = True

    def is_empty(self) -> bool:
        return len(self.buffer) == 0

    def get_buffer_size_ms(self) -> float:
        return len(self.buffer) / self.target_bytes * self.target_ms


class Clinic(BaseModel):
    id: str
    name: str
    phone_number: str | None

@app.get("/")
async def index():
    client_path = Path(__file__).parent / "static" / "index.html"
    if client_path.exists():
        return FileResponse(client_path)
    return HTMLResponse("<h1>Sarvam AI Voice Agent - Pipecat</h1><p>Client not found.</p>")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "sarvam_voice_agent_pipecat",
        "version": "3.0.0",
        "active_workers": len(workers),
    }


@app.get("/metrics")
async def metrics():
    return "# Not yet implemented\n"

# {
#   "type": "start",
#   "account_id": uuid,
#   "call_app_id": uuid,
#   "call_id": uuid,
#   "stream_id": uuid,
#   "message_id": 1,
#   "data": {
#     "encoding": "audio/l16",
#     "sample_rate": 8_000,
#     "channels": 1
#     }
# }

# {
#   "type": "audio",
#   "stream_id": uuid,
#   "message_id": integer,
#   "data": {
#     "audio_b64": {BASE64_ENCODED_AUDIO_CHUNK}
#     }
# }

# {
#   "call_id": {CALL_ID},
#   "account_id": {ACCOUNT_ID},
#   "from_number": {FROM_NUMBER},
#   "to_number": {TO_NUMBER},
#   "direction": {CALL_DIRECTION}
# }

def get_clinic_by_phone_number(phone_number: str) -> Clinic | None:
    return Clinic(
        id=DEFAULT_CLINIC_ID, 
        name="Default Clinic",
        phone_number=DEFAULT_PHONE,
    )

class Session(BaseModel):
    call_id: str
    clinic: Clinic
    from_number: str

class CallFlowRequest(BaseModel):
    call_id: str
    account_id: str
    from_number: str
    to_number: str
    # Using Literal here strictly validates that direction is either 'inbound' or 'outbound'
    direction: str
    
@app.post("/incoming-call")
async def incoming_call(payload: CallFlowRequest, request: Request) -> Dict:
    call_id = payload.call_id
    from_number = payload.from_number
    to_number = payload.to_number

    SESSIONS[call_id] = Session(
        call_id=call_id,
        clinic=get_clinic_by_phone_number(to_number),
        from_number=from_number
    )   
    
    host_url = request.url.hostname

    return {
        "action": "stream",
        "ws_url": f"wss://{host_url}/ws-telecom",
        "chunk_size": 600,
        "sample_rate": "16k"
    }

@app.websocket("/ws-telecom")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    sender_task = None
    worker = None
    session_id = "unknown"

    try:
        while True:
            message = await websocket.receive()
            # FreJun may send binary frames, ping frames, or a disconnect frame.
            # Only process text frames — skip everything else silently.
            if "text" not in message:
                if message.get("type") == "websocket.disconnect":
                    logger.info(f"WebSocket disconnect frame received: {session_id}")
                    break
                continue
            logger.debug(f"Received WebSocket message: {message}")
            message = json.loads(message["text"])

            if message["type"] == "start":
                print("Received start message:", message)
                session = SESSIONS.get(message["call_id"])
                session_id = session.call_id
                try:
                    clinic_id = session.clinic.id
                    phone = session.clinic.phone_number
                    logger.info(f"Session start: {session_id} | {clinic_id} | {phone}")

                    message_id = 1

                    # 600ms chunks: above FreJun's 500ms minimum (no crackling),
                    # small enough that barge-in overhear ≤ 600ms.
                    frejun_buffer = FreJunAudioBuffer(sample_rate=TELECOM_SAMPLE_RATE, target_ms=600)

                    # Paced send queue — dequeue one chunk, send it, then sleep 90% of
                    # its playback duration. FreJun never holds more than ~1 chunk ahead,
                    # so {"type": "clear"} on barge-in stops audio within ≤600ms.
                    audio_send_queue: asyncio.Queue = asyncio.Queue()

                    async def audio_sender_loop():
                        nonlocal message_id
                        try:
                            while True:
                                chunk = await audio_send_queue.get()
                                if chunk is None:
                                    break
                                if websocket.client_state.name == "CONNECTED":
                                    try:
                                        audio_msg = {
                                            "type": "audio",
                                            "audio_b64": base64.b64encode(chunk).decode("utf-8"),
                                            "chunk_id": message_id,
                                        }
                                        message_id += 1
                                        await websocket.send_text(json.dumps(audio_msg))
                                        logger.debug(
                                            f"Sent chunk: {len(chunk)} bytes "
                                            f"(chunk_id={message_id - 1})"
                                        )
                                    except Exception as e:
                                        logger.error(f"Audio send error: {e}")
                                # Sleep 95% of chunk playback duration — next chunk
                                # arrives at FreJun ~5% before it needs it (no gaps,
                                # half the inter-chunk silence of the old 90% factor).
                                chunk_secs = len(chunk) / (TELECOM_SAMPLE_RATE * 2)
                                await asyncio.sleep(chunk_secs * 0.95)
                        except asyncio.CancelledError:
                            pass

                    sender_task = asyncio.create_task(audio_sender_loop())

                    async def send_audio(pcm_bytes: bytes = b"", end_of_tts: bool = False):
                        try:
                            ready_chunks = frejun_buffer.add_chunk(pcm_bytes)
                            for chunk in ready_chunks:
                                await audio_send_queue.put(chunk)
                            if end_of_tts:
                                remaining = frejun_buffer.flush()
                                if remaining:
                                    await audio_send_queue.put(remaining)
                                    logger.info(
                                        f"[queue] end_of_tts tail queued: {len(remaining)}B "
                                        f"| queue depth: {audio_send_queue.qsize()}"
                                    )
                                else:
                                    logger.debug("[queue] end_of_tts: no tail to flush")
                        except Exception as e:
                            logger.error(f"Error queuing audio: {e}")

                    async def discard_audio_buffer():
                        """
                        Barge-in handler (three-step stop):
                        1. Clear server-side pacing buffer (un-chunked bytes).
                        2. Drain the send queue (chunks not yet sent to FreJun).
                        3. Send {"type": "clear"} — FreJun empties its playback queue.
                        Result: FreJun finishes the currently playing chunk (≤600ms)
                        then goes silent immediately.
                        """
                        frejun_buffer.clear()
                        drained = 0
                        while not audio_send_queue.empty():
                            try:
                                audio_send_queue.get_nowait()
                                drained += 1
                            except asyncio.QueueEmpty:
                                break
                        try:
                            if websocket.client_state.name == "CONNECTED":
                                await websocket.send_text(json.dumps({"type": "clear"}))
                                logger.info(
                                    f"Barge-in: sent FreJun clear, "
                                    f"drained {drained} queued chunks"
                                )
                        except Exception as e:
                            logger.debug(f"Barge-in clear error: {e}")

                    async def send_control(msg: dict):
                        try:
                            if websocket.client_state.name == "CONNECTED":
                                await websocket.send_text(json.dumps(msg))
                                # After sending end_call, close the WebSocket so the
                                # pipeline gets an EndFrame naturally (no idle-timeout jank).
                                if msg.get("type") == "end_call":
                                    await asyncio.sleep(0.3)  # let the message flush
                                    await websocket.close(code=1000, reason="booking complete")
                        except Exception:
                            pass

                    worker = PipecatWorker(
                        session_id=session_id,
                        clinic_id=clinic_id,
                        phone=phone,
                        on_audio_chunk=send_audio,
                        on_control_message=send_control,
                        on_barge_in=discard_audio_buffer,
                    )

                    await worker.start()

                except json.JSONDecodeError:
                    logger.error("Invalid JSON in control message")

            elif message["type"] == "audio":
                audio_b64 = message["data"]["audio_b64"]
                pcm_chunk = base64.b64decode(audio_b64)
                if worker is not None:
                    await worker.process_audio(pcm_chunk)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {session_id}")
    except RuntimeError as e:
        if "disconnect" in str(e).lower():
            logger.info(f"WebSocket disconnected (runtime): {session_id}")
        else:
            logger.error(f"WebSocket runtime error: {e}")
    except Exception as error:
        logger.error(f"WebSocket error: {error}", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        if sender_task is not None and not sender_task.done():
            sender_task.cancel()
            try:
                await sender_task
            except asyncio.CancelledError:
                pass
        if worker is not None:
            await worker.cleanup()
            del worker

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = f"session-{datetime.now().timestamp()}"
    logger.info(f"WebSocket connected: {session_id}")

    try:
        await websocket.send_text(json.dumps({
            "type": "connected",
            "session_id": session_id,
        }))

        while True:
            message = await websocket.receive()

            if "text" in message:
                try:
                    data = json.loads(message["text"])
                    msg_type = data.get("type")

                    if msg_type == "start_session":
                        clinic_id = data.get("clinic_id", DEFAULT_CLINIC_ID)
                        phone = data.get("phone", DEFAULT_PHONE)
                        logger.info(f"Session start: {session_id} | {clinic_id} | {phone}")

                        # Callback to send audio back to client
                        async def send_audio(pcm_bytes: bytes):
                            try:
                                if websocket.client_state.name == "CONNECTED":
                                    await websocket.send_bytes(pcm_bytes)
                            except Exception as e:
                                logger.error(f"Error sending audio: {e}")

                        # Callback to send control messages back to client
                        async def send_control(msg: dict):
                            try:
                                if websocket.client_state.name == "CONNECTED":
                                    await websocket.send_text(json.dumps(msg))
                                    if msg.get("type") == "end_call":
                                        await asyncio.sleep(0.3)
                                        await websocket.close(code=1000, reason="booking complete")
                            except Exception:
                                pass

                        # Create Pipecat worker (replaces SessionWorker from v2)
                        worker = PipecatWorker(
                            session_id=session_id,
                            clinic_id=clinic_id,
                            phone=phone,
                            on_audio_chunk=send_audio,
                            on_control_message=send_control,
                        )
                        workers[session_id] = worker

                        # Start the pipeline (non-blocking)
                        await worker.start()

                        await websocket.send_text(json.dumps({
                            "type": "session_ready",
                            "session_id": session_id,
                            "clinic_id": clinic_id,
                            "phone": phone,
                        }))

                    elif msg_type == "end_session":
                        logger.info(f"Session end: {session_id}")
                        if session_id in workers:
                            await workers[session_id].cleanup()
                            del workers[session_id]
                        await websocket.send_text(json.dumps({"type": "session_ended"}))

                    elif msg_type == "playback_complete":
                        # Browser finished playing TTS audio — tell pipeline
                        if session_id in workers:
                            workers[session_id].on_browser_playback_complete()

                except json.JSONDecodeError:
                    logger.error("Invalid JSON in control message")

            elif "bytes" in message:
                # Incoming PCM16 audio from browser
                pcm_chunk = message["bytes"] 
                if session_id in workers:
                    await workers[session_id].process_audio(pcm_chunk)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {session_id}")
    except RuntimeError as e:
        if "disconnect" in str(e).lower():
            logger.info(f"WebSocket disconnected (runtime): {session_id}")
        else:
            logger.error(f"WebSocket runtime error: {e}")
    except Exception as error:
        logger.error(f"WebSocket error: {error}", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            pass
    finally:
        if session_id in workers:
            await workers[session_id].cleanup()
            del workers[session_id]

print(os.getenv("TTS_SAMPLE_RATE"))
if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 60)
    logger.info("🚀 Sarvam AI Voice Agent - Pipecat Edition")
    logger.info("=" * 60)
    logger.info(f"  Server:    http://localhost:{SERVER_PORT}")
    logger.info(f"  WebSocket: ws://localhost:{SERVER_PORT}/ws")
    logger.info("=" * 60)

    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level=LOG_LEVEL.lower(),
        # reload=True
    )
