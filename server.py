"""
Sarvam AI Voice Agent — Pipecat Implementation
FastAPI server with WebSocket streaming + Pipecat pipeline
"""
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="[%(name)s %(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "19200"))
DEFAULT_CLINIC_ID = os.getenv("DEFAULT_CLINIC_ID", "clinic_001")
DEFAULT_PHONE = os.getenv("DEFAULT_PHONE", "+919999999999")

app = FastAPI(title="Sarvam AI Voice Agent - Pipecat")

from pipeline_worker import PipecatWorker

# Store active pipeline workers (replaces sessions dict from v2)
workers: dict[str, PipecatWorker] = {}


@app.get("/")
async def index():
    client_path = Path(__file__).parent / "client" / "index.html"
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
    )
