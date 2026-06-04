"""Unit tests for routes/system_status_routes.py."""
import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def stub_dependencies(monkeypatch):
    mw = types.ModuleType("core.middleware")
    mw.require_admin = lambda request: None
    monkeypatch.setitem(sys.modules, "core.middleware", mw)

    db_mod = types.ModuleType("core.database")
    db_mod.SessionLocal = MagicMock()
    db_mod.ModelEndpoint = MagicMock()
    monkeypatch.setitem(sys.modules, "core.database", db_mod)

    const_mod = types.ModuleType("src.constants")
    const_mod.SEARXNG_INSTANCE = "http://searxng-test:8080"
    monkeypatch.setitem(sys.modules, "src.constants", const_mod)

    intg_mod = types.ModuleType("src.integrations")
    intg_mod.load_integrations = lambda: []
    monkeypatch.setitem(sys.modules, "src.integrations", intg_mod)

    ep_mod = types.ModuleType("src.endpoint_resolver")
    ep_mod.build_models_url = lambda base: base.rstrip("/") + "/models"
    ep_mod.build_headers = lambda api_key, base: {"Authorization": f"Bearer {api_key}"} if api_key else {}
    monkeypatch.setitem(sys.modules, "src.endpoint_resolver", ep_mod)

    yield


def _import_module():
    if "routes.system_status_routes" in sys.modules:
        del sys.modules["routes.system_status_routes"]
    import importlib
    return importlib.import_module("routes.system_status_routes")


class TestRunProbe:
    def test_success(self):
        mod = _import_module()
        async def _ok():
            return "all good"
        result = asyncio.run(mod._run_probe("Svc", _ok()))
        assert result["ok"] is True
        assert result["detail"] == "all good"
        assert isinstance(result["latency_ms"], int)

    def test_exception_ok_false(self):
        mod = _import_module()
        async def _fail():
            raise ConnectionError("refused")
        result = asyncio.run(mod._run_probe("Svc", _fail()))
        assert result["ok"] is False
        assert "refused" in result["detail"]

    def test_unconfigured_ok_none(self):
        mod = _import_module()
        async def _unc():
            raise mod._Unconfigured("no url")
        result = asyncio.run(mod._run_probe("ntfy", _unc()))
        assert result["ok"] is None
        assert "no url" in result["detail"]


class TestChromadbProbe:
    def test_success(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("CHROMADB_HOST", "chroma-host")
        monkeypatch.setenv("CHROMADB_PORT", "8200")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        async def mock_get(url, **kwargs):
            assert "chroma-host" in url and "8200" in url
            return mock_resp
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            detail = asyncio.run(mod._chromadb_probe())
        assert "chroma-host" in detail and "8200" in detail

    def test_connection_error_propagates(self, monkeypatch):
        mod = _import_module()
        async def mock_get(url, **kwargs):
            raise ConnectionError("down")
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ConnectionError):
                asyncio.run(mod._chromadb_probe())


class TestSearxngProbe:
    def test_success(self):
        mod = _import_module()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        async def mock_get(url, **kwargs):
            return mock_resp
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            detail = asyncio.run(mod._searxng_probe())
        assert "searxng-test" in detail

    def test_5xx_raises(self):
        mod = _import_module()
        import httpx as real_httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.raise_for_status.side_effect = real_httpx.HTTPStatusError("503", request=MagicMock(), response=mock_resp)
        async def mock_get(url, **kwargs):
            return mock_resp
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(real_httpx.HTTPStatusError):
                asyncio.run(mod._searxng_probe())

    def test_unconfigured_when_instance_empty(self, monkeypatch):
        mod = _import_module()
        sys.modules["src.constants"].SEARXNG_INSTANCE = ""
        try:
            with pytest.raises(mod._Unconfigured):
                asyncio.run(mod._searxng_probe())
        finally:
            sys.modules["src.constants"].SEARXNG_INSTANCE = "http://searxng-test:8080"


class TestNtfyProbe:
    def test_unconfigured_when_no_integration(self):
        mod = _import_module()
        with pytest.raises(mod._Unconfigured):
            asyncio.run(mod._ntfy_probe())

    def test_unconfigured_when_base_url_empty(self):
        mod = _import_module()
        sys.modules["src.integrations"].load_integrations = lambda: [{"preset": "ntfy", "base_url": ""}]
        try:
            with pytest.raises(mod._Unconfigured):
                asyncio.run(mod._ntfy_probe())
        finally:
            sys.modules["src.integrations"].load_integrations = lambda: []

    def test_success(self):
        mod = _import_module()
        sys.modules["src.integrations"].load_integrations = lambda: [{"preset": "ntfy", "base_url": "http://ntfy-host:8091"}]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        async def mock_get(url, **kwargs):
            return mock_resp
        try:
            with patch("httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.get = mock_get
                mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
                mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
                detail = asyncio.run(mod._ntfy_probe())
            assert "ntfy-host" in detail
        finally:
            sys.modules["src.integrations"].load_integrations = lambda: []


class TestLlmEndpointProbes:
    def test_empty_when_no_endpoints(self):
        mod = _import_module()
        db_mod = sys.modules["core.database"]
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        db_mod.SessionLocal.return_value = mock_db
        results = asyncio.run(mod._llm_endpoint_probes())
        assert results == []

    def test_ok_endpoint(self):
        mod = _import_module()
        db_mod = sys.modules["core.database"]
        ep = MagicMock()
        ep.name = "Local Ollama"
        ep.base_url = "http://localhost:11434/v1"
        ep.api_key = None
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [ep]
        db_mod.SessionLocal.return_value = mock_db
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": ["llama3"]}
        async def mock_get(url, **kwargs):
            return mock_resp
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            results = asyncio.run(mod._llm_endpoint_probes())
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert results[0]["name"] == "Local Ollama"

    def test_unreachable_endpoint(self):
        mod = _import_module()
        db_mod = sys.modules["core.database"]
        ep = MagicMock()
        ep.name = "Bad EP"
        ep.base_url = "http://dead:9999/v1"
        ep.api_key = None
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [ep]
        db_mod.SessionLocal.return_value = mock_db
        async def mock_get(url, **kwargs):
            raise ConnectionError("refused")
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            results = asyncio.run(mod._llm_endpoint_probes())
        assert results[0]["ok"] is False


class TestSystemStatusRoute:
    def test_all_ok_false_when_service_fails(self):
        mod = _import_module()
        router = mod.setup_system_status_routes()
        handler = next(r.endpoint for r in router.routes if r.path == "/api/system/status")
        async def patched_chromadb(): raise ConnectionError("down")
        async def patched_searxng(): return "ok"
        async def patched_ntfy(): raise mod._Unconfigured("not set")
        async def patched_llm(): return []
        with (
            patch.object(mod, "_chromadb_probe", patched_chromadb),
            patch.object(mod, "_searxng_probe", patched_searxng),
            patch.object(mod, "_ntfy_probe", patched_ntfy),
            patch.object(mod, "_llm_endpoint_probes", patched_llm),
        ):
            result = asyncio.run(handler(request=MagicMock()))
        assert result["all_ok"] is False
        names = [s["name"] for s in result["services"]]
        assert "ChromaDB" in names and "SearXNG" in names and "ntfy" in names

    def test_all_ok_true_ignores_unconfigured(self):
        mod = _import_module()
        router = mod.setup_system_status_routes()
        handler = next(r.endpoint for r in router.routes if r.path == "/api/system/status")
        async def patched_chromadb(): return "Reachable at localhost:8100"
        async def patched_searxng(): return "Reachable at http://localhost:8080"
        async def patched_ntfy(): raise mod._Unconfigured("not set")
        async def patched_llm(): return [{"name": "Ollama", "ok": True, "latency_ms": 10, "detail": "HTTP 200, 2 models"}]
        with (
            patch.object(mod, "_chromadb_probe", patched_chromadb),
            patch.object(mod, "_searxng_probe", patched_searxng),
            patch.object(mod, "_ntfy_probe", patched_ntfy),
            patch.object(mod, "_llm_endpoint_probes", patched_llm),
        ):
            result = asyncio.run(handler(request=MagicMock()))
        ntfy_entry = next(s for s in result["services"] if s["name"] == "ntfy")
        assert ntfy_entry["ok"] is None
        assert result["all_ok"] is True
