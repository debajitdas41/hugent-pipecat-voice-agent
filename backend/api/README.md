# Backend SaaS API (FastAPI wrapper)

## Simple explanation (what happens now)
- The **logic** is still in `app.py` (same booking flow as before).
- FastAPI acts like a **new front door** that forwards requests to that same logic.
- The **database layer** behind the logic is now Postgres (via `db/redis_mongo.py`).
- Redis still keeps **temporary session + queue state**.

So when you call FastAPI endpoints, FastAPI **runs the old logic**, but the data is stored in **Postgres**, not Mongo.

## How requests flow
1. WhatsApp hits the FastAPI server.
2. FastAPI passes the request to the existing `app.py` logic.
3. That logic reads/writes Postgres (not Mongo) and Redis.

## Run
- `uvicorn api.main:app --reload --port 8000`

## Environment
- `SAAS_API_URL` (optional): Base URL for internal CRUD calls from Flask flow (default: http://localhost:8000).

## Notes
- Redis = short‑lived session/queue state.
- Postgres = users, clinics, appointments, check‑in/out.
- `app.py` does **not** call FastAPI endpoints; FastAPI calls `app.py`.

## API endpoints (current)
FastAPI serves two sets of routes:
1) **Native SaaS CRUD** under `/api/*`.
2) **Legacy Flask** routes mounted at `/`.

### Native SaaS CRUD (FastAPI)
#### Clinics
- POST /api/clinics
- GET /api/clinics
- GET /api/clinics/{clinic_id}
- PATCH /api/clinics/{clinic_id}

#### WhatsApp credentials
- GET /api/clinics/{clinic_id}/whatsapp-credentials
- PUT /api/clinics/{clinic_id}/whatsapp-credentials

#### Users
- POST /api/users
- GET /api/users/{user_id}
- GET /api/users/by-phone?phone=+91...
- PATCH /api/users/{user_id}

#### User clinics
- POST /api/user-clinics
- GET /api/user-clinics?user_id={uuid}&clinic_id={clinic}
- PATCH /api/user-clinics/{link_id}
- POST /api/user-clinics/{link_id}/touch
- POST /api/user-clinics/{link_id}/history
- POST /api/user-clinics/touch?user_id={uuid}&clinic_id={clinic}

#### Appointments
- POST /api/appointments
- GET /api/appointments?clinic_id=...&user_id=...
- GET /api/appointments/{appointment_id}
- PATCH /api/appointments/{appointment_id}

#### Check‑in / Check‑out
- GET /api/appointments/{appointment_id}/checkin
- POST /api/appointments/{appointment_id}/checkin
- PATCH /api/appointments/{appointment_id}/checkin
- GET /api/appointments/{appointment_id}/checkout
- POST /api/appointments/{appointment_id}/checkout
- PATCH /api/appointments/{appointment_id}/checkout

### Legacy Flask routes (mounted at /)
These are the same routes as the original Flask app, now served through FastAPI:

### Webhook
- POST /webhook
- GET /webhook

### Queue + scheduling
- GET /next
- POST /slot_tick

### Check‑in / Check‑out
- GET /checkin
- GET /checkout

### Display UI
- GET /display_data
- GET /display_ui

### Walk‑in
- GET /walkin
- POST /walkin/estimate
- POST /walkin/confirm

## SQLAlchemy ORM (introduced)
Yes. SQLAlchemy ORM is added and used in:
- db/models.py
- db/pg.py
- db/redis_mongo.py (acts as a compatibility layer so existing logic stays the same)

### What changed in storage
- Old: MongoDB collections (users, clinics, appointments)
- New: Postgres tables via SQLAlchemy
- Redis remains unchanged for sessions and queues
