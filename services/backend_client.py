"""
Backend API Client - Connects to Backend_saas for appointment booking
Copied from Local_Voice_V2/backend_client.py (unchanged)
"""
import httpx
import logging

logger = logging.getLogger("backend")


class BackendClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(self.timeout_seconds)
            limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
            self._client = httpx.AsyncClient(timeout=timeout, limits=limits)
        return self._client

    async def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        client = self._get_client()
        response = await client.request(method, url, **kwargs)
        return response

    async def get_user_by_phone(self, phone: str):
        response = await self._request("GET", "/api/users/by-phone", params={"phone": phone})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def create_user(self, phone: str, name: str | None = None):
        payload = {"phone": phone}
        if name:
            payload["name"] = name
        response = await self._request("POST", "/api/users", json=payload)
        response.raise_for_status()
        return response.json()

    async def get_or_create_user(self, phone: str, name: str | None = None):
        user = await self.get_user_by_phone(phone)
        if user:
            return user
        return await self.create_user(phone, name=name)

    async def get_user_clinic(self, user_id: str, clinic_id: str):
        response = await self._request(
            "GET",
            "/api/user-clinics",
            params={"user_id": user_id, "clinic_id": clinic_id},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def create_user_clinic(self, user_id: str, clinic_id: str):
        payload = {"user_id": user_id, "clinic_id": clinic_id}
        response = await self._request("POST", "/api/user-clinics", json=payload)
        response.raise_for_status()
        return response.json()

    async def get_or_create_user_clinic(self, user_id: str, clinic_id: str):
        link = await self.get_user_clinic(user_id, clinic_id)
        if link:
            return link
        return await self.create_user_clinic(user_id, clinic_id)

    async def get_clinic_by_phone_number_id(self, phone_number_id: str):
        response = await self._request(
            "GET",
            "/api/clinics/by-phone-number-id",
            params={"phone_number_id": phone_number_id},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_clinic(self, clinic_id: str):
        response = await self._request("GET", f"/api/clinics/{clinic_id}")
        response.raise_for_status()
        return response.json()

    async def update_user_clinic(self, link_id: int, payload: dict):
        response = await self._request("PATCH", f"/api/user-clinics/{link_id}", json=payload)
        response.raise_for_status()
        return response.json()

    async def slot_availability(self, clinic_id: str, date: str, time: str):
        response = await self._request(
            "GET",
            "/api/slots/availability",
            params={"clinic_id": clinic_id, "date": date, "time": time},
        )
        response.raise_for_status()
        return response.json()

    async def available_slots(self, clinic_id: str, date: str):
        response = await self._request(
            "GET",
            "/api/slots/available",
            params={"clinic_id": clinic_id, "date": date},
        )
        response.raise_for_status()
        return response.json()

    async def reserve_appointment(self, payload: dict):
        """Reserve appointment - returns 409 if slot is full"""
        response = await self._request("POST", "/api/appointments/reserve", json=payload)
        return response

    async def update_appointment(self, appointment_id: str, payload: dict):
        response = await self._request("PATCH", f"/api/appointments/{appointment_id}", json=payload)
        response.raise_for_status()
        return response.json()

    async def send_checkin_qr(self, appointment_id: str):
        response = await self._request("POST", f"/api/appointments/{appointment_id}/send-qr")
        response.raise_for_status()
        return response.json()

    async def list_appointments(
        self,
        clinic_id: str | None = None,
        user_id: str | None = None,
        phone: str | None = None,
    ):
        params = {}
        if clinic_id:
            params["clinic_id"] = clinic_id
        if user_id:
            params["user_id"] = user_id
        if phone:
            params["phone"] = phone
        response = await self._request("GET", "/api/appointments", params=params)
        response.raise_for_status()
        return response.json()

    async def get_session(self, phone: str, clinic_id: str):
        response = await self._request(
            "GET",
            "/api/sessions",
            params={"phone": phone, "clinic_id": clinic_id},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def create_session(self, phone: str, clinic_id: str, data: dict | None = None, ttl: int | None = None):
        payload = {"phone": phone, "clinic_id": clinic_id}
        if data:
            payload["data"] = data
        if ttl:
            payload["ttl"] = ttl
        response = await self._request("POST", "/api/sessions/create", json=payload)
        response.raise_for_status()
        return response.json()

    async def update_session(self, phone: str, clinic_id: str, data: dict, ttl: int | None = None):
        params = {"phone": phone, "clinic_id": clinic_id}
        payload = {"data": data}
        if ttl:
            payload["ttl"] = ttl
        response = await self._request("PATCH", "/api/sessions", params=params, json=payload)
        response.raise_for_status()
        return response.json()

    async def close(self):
        """Close the HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None
