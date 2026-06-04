"""System service health probes — GET /api/system/status (admin only).

Each bundled service gets an independent async probe. _run_probe() wraps
any probe into a normalised {name, ok, latency_ms, detail} dict:
  ok=True   — reachable and healthy
  ok=False  — configured but unreachable / error
  ok=None   — not configured (raise _Unconfigured from probe); not a failure
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Request

from core.middleware import require_admin

logger = logging.getLogger(__name__)

# Per-probe HTTP timeout in seconds. Keeps the endpoint responsive even when
# a bundled service is completely unreachable (no TCP reset, just silence).
_PROBE_TIMEOUT = float(os.getenv("SYSTEM_STATUS_TIMEOUT", "3.0"))


class _Unconfigured(Exception):
    """Raised by a probe when the service has no configuration entry.
    _run_probe treats this as ok=None rather than ok=False."""


async def _run_probe(name: str, coro) -> Dict[str, Any]:
    """Run *coro*, returning a normalised probe result dict.

    Catches _Unconfigured → ok=None, any other exception → ok=False.
    The coroutine should return a human-readable detail string on success.
    """
    t0 = time.monotonic()
    try:
        detail = await coro
        return {
            "name": name,
            "ok": True,
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "detail": str(detail),
        }
    except _Unconfigured as exc:
        return {
            "name": name,
            "ok": None,
            "latency_ms": 0,
            "detail": str(exc),
        }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "latency_ms": round((time.monotonic() - t0) * 1000),
            "detail": str(exc),
        }


async def _chromadb_probe() -> str:
    """Probe ChromaDB by hitting its heartbeat endpoint."""
    host = os.getenv("CHROMADB_HOST", "localhost")
    port = int(os.getenv("CHROMADB_PORT", "8100"))
    url = f"http://{host}:{port}/api/v2/heartbeat"
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        r = await client.get(url)
        r.raise_for_status()
    return f"Reachable at {host}:{port}"


async def _searxng_probe() -> str:
    """Probe SearXNG by fetching its root page (any 2xx/3xx/4xx = alive)."""
    from src.constants import SEARXNG_INSTANCE
    base = SEARXNG_INSTANCE.rstrip("/")
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        r = await client.get(base + "/")
        # 5xx = server error (raise); anything below = server is responding
        if r.status_code >= 500:
            r.raise_for_status()
    return f"Reachable at {base}"


async def _ntfy_probe() -> str:
    """Probe the ntfy server configured under Integrations.

    Raises _Unconfigured if no ntfy integration has been added yet so that
    the frontend shows a neutral 'not configured' indicator rather than a
    red failure.
    """
    from src.integrations import load_integrations
    integrations = load_integrations()
    ntfy = next(
        (i for i in integrations
         if (i.get("preset") or i.get("name", "")).lower() == "ntfy"),
        None,
    )
    if not ntfy:
        raise _Unconfigured("Not configured — add ntfy in Settings > Integrations")
    base_url = (ntfy.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise _Unconfigured("ntfy base URL is empty — edit the integration in Settings > Integrations")
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        r = await client.get(base_url + "/")
        if r.status_code >= 500:
            r.raise_for_status()
    return f"Reachable at {base_url}"


async def _llm_endpoint_probes() -> List[Dict[str, Any]]:
    """Probe every enabled LLM endpoint, returning one result dict per endpoint.

    Each dict has the same shape as _run_probe output so the frontend can
    render them uniformly alongside the bundled-service rows.
    """
    from core.database import SessionLocal, ModelEndpoint
    from src.endpoint_resolver import build_models_url, build_headers

    db = SessionLocal()
    try:
        endpoints = (
            db.query(ModelEndpoint)
            .filter(ModelEndpoint.is_enabled.is_(True))
            .all()
        )
    finally:
        db.close()

    if not endpoints:
        return []

    async def _probe_one(ep) -> Dict[str, Any]:
        label = ep.name or ep.base_url
        models_url = build_models_url(ep.base_url)
        headers = build_headers(ep.api_key, ep.base_url)
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
                r = await client.get(models_url, headers=headers)
            ok = r.status_code < 400
            count = None
            try:
                body = r.json()
                models_list = body.get("models") or body.get("data") or []
                count = len(models_list)
            except Exception:
                pass
            detail = f"HTTP {r.status_code}"
            if count is not None:
                detail += f", {count} model{'s' if count != 1 else ''}"
            return {
                "name": label,
                "ok": ok,
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "detail": detail,
            }
        except Exception as exc:
            return {
                "name": label,
                "ok": False,
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "detail": str(exc),
            }

    return list(await asyncio.gather(*[_probe_one(ep) for ep in endpoints]))


def setup_system_status_routes() -> APIRouter:
    """Return a router with GET /api/system/status."""
    router = APIRouter(tags=["system"])

    @router.get("/api/system/status")
    async def system_status(request: Request) -> Dict[str, Any]:
        """Return reachability and latency for all configured services.

        Runs all probes concurrently. Response shape:
          {
            "services": [
              {"name": str, "ok": bool|null, "latency_ms": int, "detail": str},
              ...
            ],
            "all_ok": bool   # True only when every *configured* service is up
          }
        ok=null means the service is not configured — excluded from all_ok.
        """
        require_admin(request)

        bundled = await asyncio.gather(
            _run_probe("ChromaDB", _chromadb_probe()),
            _run_probe("SearXNG", _searxng_probe()),
            _run_probe("ntfy", _ntfy_probe()),
        )

        llm_results = await _llm_endpoint_probes()

        services: List[Dict[str, Any]] = list(bundled) + llm_results

        # only configured (ok != None) services count toward all_ok
        configured = [s for s in services if s["ok"] is not None]
        all_ok = bool(configured) and all(s["ok"] for s in configured)

        return {"services": services, "all_ok": all_ok}

    return router
