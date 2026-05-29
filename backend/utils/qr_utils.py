# utils/qr_utils.py
import qrcode
from io import BytesIO

def generate_qr_bytes(data: str) -> bytes:
    """
    Generate a QR code (PNG) for the given text and return raw bytes.
    """
    qr = qrcode.make(data)
    buffer = BytesIO()
    qr.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
