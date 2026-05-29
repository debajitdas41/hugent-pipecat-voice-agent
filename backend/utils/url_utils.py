import os


def get_public_base_url() -> str:
    """Return the public base URL used for QR links and callbacks."""
    for key in ("PUBLIC_BASE_URL", "Backend_WORKER_URL", "SAAS_API_URL"):
        value = os.getenv(key)
        if value:
            return value.rstrip("/")
    return "http://localhost:8000"
