from __future__ import annotations

import asyncio
import logging
import math
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp
import segno
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Server, get_settings
from app.db import Session, Subscription, get_subscription_by_uuid, init_db, utcnow
from app.vpn import (
    VpnAPIError,
    close_all,
    extract_expiry,
    extract_traffic,
    extract_uuid,
    get_vpn,
)

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_SUB_FORMATS = frozenset({"raw", "json", "clash", "sing-box"})
_FORWARD_HEADERS = (
    "Content-Disposition",
    "Subscription-Userinfo",
    "Profile-Update-Interval",
    "Profile-Title",
    "Profile-Web-Page-Url",
    "Support-Url",
)
_PROXY_TIMEOUT = aiohttp.ClientTimeout(total=25)

_http: aiohttp.ClientSession | None = None


def _http_session() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=_PROXY_TIMEOUT)
    return _http


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await init_db()
    logger.info("Web app started, %d server(s) configured", len(get_settings().servers))
    yield
    if _http is not None and not _http.closed:
        await _http.close()
    await close_all()


app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


def _ru_days(n: int) -> str:
    if n % 100 in (11, 12, 13, 14):
        return "дней"
    if n % 10 == 1:
        return "день"
    if n % 10 in (2, 3, 4):
        return "дня"
    return "дней"


def _fmt_gb(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


async def _resolve_server(uuid: str) -> tuple[Server | None, Subscription | None]:
    settings = get_settings()
    sub: Subscription | None = None
    try:
        async with Session() as session:
            sub = await get_subscription_by_uuid(session, uuid)
    except Exception:
        logger.exception("DB lookup failed for uuid %s", uuid)
    if sub is not None:
        server = settings.server(sub.server_key)
        if server is not None:
            return server, sub
    for server in settings.servers:
        try:
            if await get_vpn(server.key).sub_json(uuid) is not None:
                return server, sub
        except VpnAPIError as exc:
            logger.warning("Probe of server %s for uuid %s failed: %s", server.key, uuid, exc)
    return None, sub


def _proxy_error(status: int, message_ru: str, as_json: bool, code: str) -> Response:
    headers = {"Cache-Control": "no-store"}
    if as_json:
        return JSONResponse(
            {"ok": False, "error": code, "message": message_ru},
            status_code=status,
            headers=headers,
        )
    return PlainTextResponse(message_ru, status_code=status, headers=headers)


async def _proxy(uuid: str, fmt: str) -> Response:
    as_json = fmt == "json"
    if not _UUID_RE.match(uuid):
        return _proxy_error(404, "Подписка не найдена.", as_json, "not_found")
    server, _ = await _resolve_server(uuid)
    if server is None:
        return _proxy_error(404, "Подписка не найдена.", as_json, "not_found")

    url = f"{server.sub_url}/sub/{uuid}" + (f"/{fmt}" if fmt else "")
    kwargs: dict[str, Any] = {} if server.verify_ssl else {"ssl": False}
    try:
        async with _http_session().get(url, **kwargs) as resp:
            body = await resp.read()
            if resp.status == 404:
                return _proxy_error(404, "Подписка не найдена.", as_json, "not_found")
            if not 200 <= resp.status < 300:
                raise VpnAPIError(resp.status, "sub_error", f"GET {url} -> {resp.status}")
            headers = {"Cache-Control": "no-store"}
            for name in _FORWARD_HEADERS:
                if name in resp.headers:
                    headers[name] = resp.headers[name]
            media_type = resp.headers.get("Content-Type") or "text/plain; charset=utf-8"
            return Response(content=body, media_type=media_type, headers=headers)
    except (VpnAPIError, aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Sub proxy failed for uuid %s (server %s)", uuid, server.key)
        return _proxy_error(
            502,
            "Сервер подписки временно недоступен, попробуйте позже.",
            as_json,
            "backend_unavailable",
        )


def _base_context() -> dict[str, Any]:
    settings = get_settings()
    return {
        "shop_name": settings.shop_name,
        "support_url": settings.support_url,
        "not_found": False,
    }


def _render_not_found(request: Request) -> HTMLResponse:
    ctx = _base_context()
    ctx["not_found"] = True
    return templates.TemplateResponse(
        request,
        "page.html",
        ctx,
        status_code=404,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/{uuid}/sub")
async def sub_default(uuid: str) -> Response:
    return await _proxy(uuid, "")


@app.get("/{uuid}/{fmt}")
async def sub_format(uuid: str, fmt: str) -> Response:
    if fmt not in _SUB_FORMATS:
        return _proxy_error(404, "Неизвестный формат подписки.", False, "not_found")
    return await _proxy(uuid, fmt)


@app.get("/{uuid}", response_class=HTMLResponse)
async def subscription_page(request: Request, uuid: str) -> Response:
    if not _UUID_RE.match(uuid):
        return _render_not_found(request)

    server, sub = await _resolve_server(uuid)
    if server is None:
        return _render_not_found(request)

    client: dict[str, Any] | None = None
    if sub is not None:
        try:
            client = await get_vpn(server.key).get_client(sub.client_name)
        except VpnAPIError:
            logger.exception(
                "get_client failed for %s on server %s", sub.client_name, server.key
            )
    else:
        try:
            for candidate in await get_vpn(server.key).clients():
                if (extract_uuid(candidate) or "").lower() == uuid.lower():
                    client = candidate
                    break
        except VpnAPIError:
            logger.exception("clients() failed on server %s", server.key)

    now = utcnow()
    expires_at = sub.expires_at if sub is not None else None
    if expires_at is None and client is not None:
        expires_at = extract_expiry(client)

    active = True
    days_left: int | None = None
    expires_str: str | None = None
    if expires_at is not None:
        active = expires_at > now
        expires_str = (expires_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
        days_left = max(0, math.ceil((expires_at - now).total_seconds() / 86400))

    used, limit = extract_traffic(client) if client is not None else (None, None)
    if limit is None and sub is not None and sub.traffic_gb > 0:
        limit = float(sub.traffic_gb)

    traffic: dict[str, Any] | None = None
    if used is not None and limit is not None and limit > 0:
        percent = min(100, max(0, round(used / limit * 100)))
        traffic = {
            "mode": "bar",
            "used": _fmt_gb(used),
            "limit": _fmt_gb(limit),
            "percent": percent,
        }
    elif used is not None:
        traffic = {"mode": "unlimited_used", "used": _fmt_gb(used)}
    elif limit is not None and limit > 0:
        traffic = {"mode": "limit_only", "limit": _fmt_gb(limit)}
    elif sub is not None and sub.traffic_gb == 0:
        traffic = {"mode": "unlimited"}

    devices_str: str | None = None
    if sub is not None:
        devices_str = "Без ограничений" if sub.devices <= 0 else str(sub.devices)

    name_mask: str | None = None
    if sub is not None and sub.client_name:
        name_mask = sub.client_name[:2] + "••"

    settings = get_settings()
    sub_url = f"{settings.public_base_url}/{uuid}/sub"
    qr_data_uri = segno.make(sub_url, error="m").svg_data_uri(
        scale=4, border=2, dark="#101623", light="#ffffff"
    )

    ctx = _base_context()
    ctx.update(
        {
            "active": active,
            "days_left": days_left,
            "days_word": _ru_days(days_left) if days_left is not None else "",
            "expires_str": expires_str,
            "traffic": traffic,
            "devices_str": devices_str,
            "name_mask": name_mask,
            "sub_url": sub_url,
            "qr_data_uri": qr_data_uri,
        }
    )
    return templates.TemplateResponse(
        request, "page.html", ctx, headers={"Cache-Control": "no-store"}
    )
