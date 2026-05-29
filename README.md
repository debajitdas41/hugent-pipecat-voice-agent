# 1. Clone
git clone  https://github.com/debajitdas41/hugent-pipecat-voice-agent.git
cd pipecat-voice-agent

# 2. Backend — one time setup
cd backend
cp .env.example .env          # set APP_SECRET to any random string
docker-compose up -d --build  # starts postgres + redis + api + worker
docker exec clinic-backend-api alembic upgrade head   # run DB migrations
docker exec clinic-backend-api python seed_clinic.py  # seed test clinic
cd ..

# 3. Voice agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set SARVAM_API_KEY
python test_setup.py          # smoke test (all 5 must pass)
python server.py              # start voice agent on :19200

# 4. Test
# Open http://localhost:19200 → Start Session → speak







# Pipecat Voice Agent — Sarvam AI

AI voice agent for clinic appointment booking.  
**Stack**: [Pipecat](https://github.com/pipecat-ai/pipecat) · [Sarvam AI](https://sarvam.ai) (STT + TTS + LLM) · FastAPI WebSocket  
**Languages**: English, Hindi, Bengali, Tamil, Telugu, Kannada, Malayalam, Marathi, Gujarati, Punjabi, Odia

---

## What Runs Where

| Component | How |
|-----------|-----|
| **Voice agent** (`server.py`) | Python directly — no Docker |
| **Backend API** (appointments, slots, users) | Docker Compose |
| **PostgreSQL** (persistent data) | Docker Compose — included |
| **Redis** (session + queue state) | Docker Compose — included |

---

## Repository Structure

```
pipecat-voice-agent/
│
├── server.py                 # FastAPI + WebSocket server (port 19200)
├── pipeline_worker.py        # Pipecat pipeline per voice session
├── booking_manager.py        # Appointment booking state machine
├── extraction.py             # Field extraction from natural speech
├── backend_client.py         # HTTP client → Backend API
├── name_correction.py        # Indian name STT error correction
├── context.py                # Dynamic LLM system-prompt builder
├── llm.py                    # Raw Sarvam LLM HTTP wrapper
├── stt.py                    # STT helpers
├── tts.py                    # TTS helpers
├── run.sh                    # Quick-start script
├── requirements.txt          # Voice agent Python deps
├── .env.example              # Voice agent env vars (copy → .env)
├── test_setup.py             # Pre-flight smoke test
│
├── processors/               # Custom Pipecat frame processors
│   ├── audio_input.py        # WebSocket bytes → AudioRawFrame
│   ├── audio_output.py       # AudioRawFrame → WebSocket bytes
│   ├── booking_processor.py  # Business logic bridge (non-blocking)
│   ├── assistant_capture.py  # Barge-in / interruption handling
│   └── sarvam_m_llm.py       # sarvam-m LLM processor (custom)
│
├── prompts/                  # System prompts — 11 languages
│   ├── loader.py
│   └── en.py hi.py bn.py ta.py te.py kn.py ml.py mr.py gu.py pa.py od.py
│
├── client/
│   └── index.html            # Browser mic/speaker WebSocket client
│
└── backend/                  # Backend stack — Docker only
    ├── docker-compose.yml    # ← postgres + redis + api + worker
    ├── .env.example          # ← copy to .env before running
    ├── seed_clinic.py        # ← creates test clinic in DB
    ├── Dockerfile
    ├── requirements.txt
    ├── alembic.ini
    ├── alembic/versions/     # DB migration scripts
    ├── app.py                # Flask webhook app (WhatsApp flows)
    ├── scheduler.py          # APScheduler slot-tick worker
    ├── redis_client.py
    ├── session_store.py
    ├── api/                  # FastAPI routers (REST API)
    ├── db/                   # SQLAlchemy models + Redis helpers
    ├── utils/                # Slot utils, QR, WhatsApp utils
    ├── routes/               # Flask route blueprints
    ├── services/             # Business logic services
    ├── flows/                # WhatsApp conversation flows
    └── queue_service/        # Live queue engine
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Python 3.10+** | For the voice agent |
| **Docker Desktop** | For the backend stack |
| **Sarvam AI API key** | [sarvam.ai](https://sarvam.ai) — free tier available |
| **Chrome / Firefox** | For the browser mic client |

---

## Setup — Step by Step

### Step 1 · Clone

```bash
git clone https://github.com/YOUR_USERNAME/pipecat-voice-agent.git
cd pipecat-voice-agent
```

---

### Step 2 · Start the Backend (Docker)

```bash
cd backend

# Configure backend environment
cp .env.example .env
```

Open `backend/.env` and set `APP_SECRET` to any random hex string:
```bash
# Quick way to generate one:
python3 -c "import secrets; print(secrets.token_hex(16))"
```

Start all 4 services (Postgres + Redis + API + Worker):
```bash
docker-compose up -d --build
```

> First build takes 2–3 minutes. Subsequent starts are instant.

Wait for Postgres to be ready (~10s), then run DB migrations:
```bash
docker exec clinic-backend-api alembic upgrade head
```

Seed the test clinic:
```bash
docker exec clinic-backend-api python seed_clinic.py
```

Expected output:
```
✓ Created clinic : clinic_001 — SmileBright Dental Care
  Departments    : Dental, Skin, Ortho, ENT
  Slots          : 00:00 – 23:45, every 30 min, 2 patients/slot
```

Verify the backend is up:
```bash
curl http://localhost:8000/health
# {"status": "ok"}

curl http://localhost:8000/api/clinics/clinic_001
# {"id": "clinic_001", "name": "City Care Clinic", "departments": [...], ...}
```

```bash
cd ..   # back to repo root
```

---

### Step 3 · Set Up the Voice Agent

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate      # Linux / macOS
# .\venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
```

Edit `.env` — minimum required:
```env
# Your Sarvam AI API key (get from https://sarvam.ai)
SARVAM_API_KEY=your_sarvam_api_key_here

# Backend API — the Docker container started above
BACKEND_API_BASE_URL=http://localhost:8000

# Test clinic created by seed_clinic.py
DEFAULT_CLINIC_ID=clinic_001
DEFAULT_PHONE=+919999999999

# Server
SERVER_HOST=0.0.0.0
SERVER_PORT=19200

# STT
STT_LANGUAGE=unknown      # auto-detect caller language
STT_MODEL=saaras:v3

# TTS
TTS_MODEL=bulbul:v3
TTS_SPEAKER=priya
TTS_TARGET_LANGUAGE=en-IN

# LLM
LLM_MODEL=sarvam-m
LLM_TIMEOUT_MS=12000

DEFAULT_TIMEZONE=Asia/Kolkata
```

Run the smoke test to verify everything is wired correctly:
```bash
python test_setup.py
```

All 5 checks must pass before proceeding.

---

### Step 4 · Start the Voice Agent

```bash
python server.py
```



You should see:
```
INFO:     Uvicorn running on http://0.0.0.0:19200
```

---

### Step 5 · Test in Browser

Open **http://localhost:19200** in Chrome or Firefox.

1. Click **Start Session**
2. Speak: *"I want to book an appointment"*
3. The agent will collect fields in order:
   - Name → Age → Department → Date → Time → Confirm

> **Mic permission**: The browser requires `localhost` (or HTTPS) for microphone access.  
> Do **not** use `127.0.0.1` or the machine IP directly.

---

## Stopping Everything

```bash
# Stop voice agent
Ctrl+C

# Stop backend
cd backend
docker-compose down

# Stop AND delete all DB/Redis data (clean slate)
docker-compose down -v
```

---

## Architecture

```
Browser (mic + speaker)
        │
        │  WebSocket  (PCM16 binary audio frames)
        │
   server.py  ── FastAPI, port 19200
        │
   pipeline_worker.py  ── one Pipecat pipeline per session
        │
   ┌────▼─────────────────────────────────────────┐
   │  AudioInputProcessor                          │
   │       ↓                                       │
   │  SarvamSTTService  (saaras:v3)                │
   │       ↓                                       │
   │  BookingProcessor  ←──── HTTP ──→ Backend API │
   │       ↓                          (port 8000)  │
   │  SarvamMLLMProcessor (sarvam-m)               │
   │       ↓                                       │
   │  SarvamTTSService  (bulbul:v3)                │
   │       ↓                                       │
   │  AudioOutputProcessor                         │
   └────┬─────────────────────────────────────────┘
        │
        │  PCM16 audio chunks
        │
   Browser (plays response)
```

### Backend Data Flow
```
Backend API (port 8000)
    ├── PostgreSQL (port 5432)  — clinics, users, appointments
    └── Redis      (port 6379)  — voice session state, queue state
```

---

## Configuration Reference

### TTS Voices

| Model | Speaker Options |
|-------|----------------|
| `bulbul:v3` ✅ recommended | aditya, ritu, **priya**, neha, rahul, pooja, rohan, simran, kavya, amit, dev, ishita, shreya |
| `bulbul:v2` | anushka, abhilash, manisha, vidya, arya, karun, hitesh |

### Language Codes for TTS_TARGET_LANGUAGE / STT_LANGUAGE

| Language | Code |
|----------|------|
| English (India) | `en-IN` |
| Hindi | `hi-IN` |
| Bengali | `bn-IN` |
| Tamil | `ta-IN` |
| Telugu | `te-IN` |
| Kannada | `kn-IN` |
| Malayalam | `ml-IN` |
| Marathi | `mr-IN` |
| Gujarati | `gu-IN` |
| Punjabi | `pa-IN` |
| Odia | `od-IN` |

Set `STT_LANGUAGE=unknown` to let Sarvam auto-detect the caller's language on every turn.

### LLM Models

| Model | Speed | Use case |
|-------|-------|---------|
| `sarvam-m` ✅ | Fastest | Real-time voice conversation |
| `sarvam-30b` | Medium | Higher quality responses |
| `sarvam-105b` | Slow | Best quality |

---

## Troubleshooting

**`SARVAM_API_KEY not set`**  
→ Copy `.env.example` to `.env` and add your key from [sarvam.ai](https://sarvam.ai)

**`Connection refused` to backend**  
→ Check containers are running: `docker ps`  
→ Should see: `clinic-backend-api` (port 8000), `hugent-postgres` (5432), `hugent-redis` (6379)

**`clinic_001 not found` error**  
→ Re-run: `docker exec clinic-backend-api python seed_clinic.py`

**Migrations not applied / table does not exist**  
→ `docker exec clinic-backend-api alembic upgrade head`

**No microphone / browser mic not working**  
→ Must use `http://localhost:19200` — not the IP address  
→ Chrome/Firefox will ask for permission the first time

**Agent responds but says "slot not available"**  
→ Check that `DEFAULT_CLINIC_ID=clinic_001` in `.env` and the backend is running

**Want to reset all data (fresh start)**  
```bash
cd backend
docker-compose down -v          # deletes postgres volume
docker-compose up -d
docker exec clinic-backend-api alembic upgrade head
docker exec clinic-backend-api python seed_clinic.py
```

---

## Backend API Endpoints Used by Voice Agent

`backend_client.py` calls these endpoints on `BACKEND_API_BASE_URL`:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/clinics/{id}` | Load clinic info + departments + slot settings |
| GET | `/api/slots/availability` | Check if a specific slot is free |
| GET | `/api/slots/available` | List all available slots for a date |
| POST | `/api/appointments/reserve` | Book the appointment |
| PATCH | `/api/appointments/{id}` | Update appointment details |
| POST | `/api/appointments/{id}/send-qr` | Send check-in QR via WhatsApp |
| GET | `/api/users/by-phone` | Look up patient by phone number |
| POST | `/api/users` | Register a new patient |
| GET | `/api/user-clinics` | Get patient ↔ clinic relationship |
| POST | `/api/user-clinics` | Create patient ↔ clinic link |
| PATCH | `/api/user-clinics/{id}` | Update patient ↔ clinic record |
| GET | `/api/sessions` | Load voice session state from Redis |
| POST | `/api/sessions/create` | Create new session in Redis |
| PATCH | `/api/sessions` | Update session state in Redis |
