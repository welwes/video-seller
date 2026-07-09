from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from app.config import Server, get_settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class VpnAPIError(Exception):
    def __init__(self, status: int, code: str = "", message: str = "") -> None:
        self.status = status
        self.code = code or "unknown_error"
        super().__init__(message or f"VPN API error {status}: {self.code}")


class VpnClient:
    def __init__(self, server: Server) -> None:
        self.server = server
        self._api_base = server.api_url.rstrip("/")
        self._sub_base = server.sub_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None


    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=_REQUEST_TIMEOUT,
                headers={"Authorization": f"Bearer {self.server.api_token}"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


    async def _request(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> tuple[int, Any]:
        session = self._get_session()
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if not self.server.verify_ssl:
            kwargs["ssl"] = False
        try:
            async with session.request(method, url, **kwargs) as resp:
                try:
                    body: Any = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = await resp.text()
                if resp.status == 404 and allow_404:
                    return resp.status, body
                if not 200 <= resp.status < 300:
                    code = ""
                    if isinstance(body, dict):
                        code = str(
                            body.get("error") or body.get("code") or body.get("detail") or ""
                        )
                    raise VpnAPIError(resp.status, code, f"{method} {url} -> {resp.status} {code}")
                return resp.status, body
        except VpnAPIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise VpnAPIError(0, "network_error", f"{method} {url} failed: {exc}") from exc

    async def _api(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> tuple[int, Any]:
        return await self._request(method, f"{self._api_base}/api{path}", json, allow_404)


    async def health(self) -> dict:
        _, body = await self._api("GET", "/health")
        return body if isinstance(body, dict) else {}

    async def status(self) -> dict:
        _, body = await self._api("GET", "/status")
        return body if isinstance(body, dict) else {}

    async def clients(self) -> list[dict]:
        _, body = await self._api("GET", "/clients")
        if isinstance(body, list):
            return [c for c in body if isinstance(c, dict)]
        if isinstance(body, dict):
            for key in ("clients", "users", "data", "items"):
                value = body.get(key)
                if isinstance(value, list):
                    return [c for c in value if isinstance(c, dict)]
        return []

    async def get_client(self, name: str) -> dict | None:
        status, body = await self._api("GET", f"/clients/{name}", allow_404=True)
        if status == 404:
            return None
        if isinstance(body, dict):
            for key in ("client", "user", "data"):
                value = body.get(key)
                if isinstance(value, dict):
                    return value
            return body
        return None

    async def create(self, name: str, days: int, traffic_gb: int = 0, devices: int = 0) -> dict:
        payload: dict[str, Any] = {"name": name, "days": days}
        if traffic_gb:
            payload["traffic_limit_gb"] = traffic_gb
        if devices:
            payload["device_limit"] = devices
        _, body = await self._api("POST", "/create", json=payload)
        return body if isinstance(body, dict) else {}

    async def edit(
        self,
        name: str,
        days: int | None = None,
        traffic_gb: int | None = None,
        devices: int | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"name": name}
        if days is not None:
            payload["days"] = days
        if traffic_gb is not None:
            payload["traffic_limit_gb"] = traffic_gb
        if devices is not None:
            payload["device_limit"] = devices
        _, body = await self._api("PATCH", "/edit", json=payload)
        return body if isinstance(body, dict) else {}

    async def ban(self, name: str, reason: str = "") -> dict:
        _, body = await self._api("PATCH", f"/clients/{name}/ban", json={"reason": reason})
        return body if isinstance(body, dict) else {}

    async def unban(self, name: str) -> dict:
        _, body = await self._api("PATCH", f"/clients/{name}/unban")
        return body if isinstance(body, dict) else {}

    async def delete(self, name: str) -> dict:
        _, body = await self._api("DELETE", f"/clients/{name}")
        return body if isinstance(body, dict) else {}


    def _sub_url_for(self, uuid: str, fmt: str = "") -> str:
        url = f"{self._sub_base}/sub/{uuid}"
        if fmt:
            url += f"/{fmt}"
        return url

    async def sub_text(self, uuid: str, fmt: str = "") -> str:
        session = self._get_session()
        kwargs: dict[str, Any] = {}
        if not self.server.verify_ssl:
            kwargs["ssl"] = False
        url = self._sub_url_for(uuid, fmt)
        try:
            async with session.get(url, **kwargs) as resp:
                text = await resp.text()
                if not 200 <= resp.status < 300:
                    raise VpnAPIError(resp.status, "sub_error", f"GET {url} -> {resp.status}")
                return text
        except VpnAPIError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise VpnAPIError(0, "network_error", f"GET {url} failed: {exc}") from exc

    async def sub_json(self, uuid: str) -> dict | None:
        status, body = await self._request(
            "GET", self._sub_url_for(uuid, "json"), allow_404=True
        )
        if status == 404:
            return None
        return body if isinstance(body, dict) else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0:
            return None
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        num = _as_float(raw)
        if num is not None and raw.replace(".", "", 1).lstrip("+-").isdigit():
            return _parse_datetime(num)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


def extract_uuid(client: dict) -> str | None:
    for key in ("uuid", "id", "client_id"):
        value = client.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_expiry(client: dict) -> datetime | None:
    for key in ("expires_at", "expiry", "expire", "expire_at", "expiration"):
        dt = _parse_datetime(client.get(key))
        if dt is not None:
            return dt
    for key in ("days_left", "days_remaining"):
        num = _as_float(client.get(key))
        if num is not None:
            return _utcnow() + timedelta(days=num)
    return None


def extract_days_left(client: dict) -> int | None:
    for key in ("days_left", "days_remaining"):
        num = _as_float(client.get(key))
        if num is not None:
            return max(0, math.ceil(num))
    expiry = extract_expiry(client)
    if expiry is not None:
        seconds = (expiry - _utcnow()).total_seconds()
        return max(0, math.ceil(seconds / 86400))
    return None


def extract_traffic(client: dict) -> tuple[float | None, float | None]:
    used: float | None = None
    for key in ("traffic_used_gb", "used_gb"):
        used = _as_float(client.get(key))
        if used is not None:
            break
    if used is None:
        up = _as_float(client.get("up"))
        down = _as_float(client.get("down"))
        if up is not None or down is not None:
            used = ((up or 0.0) + (down or 0.0)) / (1024**3)

    limit: float | None = None
    for key in ("traffic_limit_gb", "limit_gb"):
        limit = _as_float(client.get(key))
        if limit is not None:
            break
    if limit is not None and limit <= 0:
        limit = None
    return used, limit


_clients: dict[str, VpnClient] = {}


def get_vpn(server_key: str) -> VpnClient:
    client = _clients.get(server_key)
    if client is None:
        server = get_settings().server(server_key)
        if server is None:
            raise ValueError(f"Unknown server key: {server_key!r}")
        client = VpnClient(server)
        _clients[server_key] = client
    return client


async def close_all() -> None:
    for client in list(_clients.values()):
        try:
            await client.close()
        except Exception:
            logger.exception("Failed to close VPN client for %s", client.server.key)
    _clients.clear()
