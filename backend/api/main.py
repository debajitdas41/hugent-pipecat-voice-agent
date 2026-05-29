from fastapi import FastAPI
from starlette.middleware.wsgi import WSGIMiddleware

import app as flask_app

from .routers import appointments, checkins, checkouts, clinics, slots, user_clinics, users, sessions

app = FastAPI(title="ChaloAI SaaS API")

app.include_router(clinics.router)
app.include_router(users.router)
app.include_router(user_clinics.router)
app.include_router(appointments.router)
app.include_router(checkins.router)
app.include_router(checkouts.router)
app.include_router(slots.router)
app.include_router(sessions.router)


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", WSGIMiddleware(flask_app.app))
