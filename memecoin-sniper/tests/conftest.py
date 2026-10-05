"""Tests must never reach the real network: fail loudly if one tries."""
import httpx
import pytest

_real = httpx.AsyncHTTPTransport.handle_async_request
LOCAL = ("127.0.0.1", "localhost", "::1")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    async def guarded(self, request):
        if request.url.host not in LOCAL:
            raise AssertionError(f"test tried to reach the real network: {request.url.host}")
        return await _real(self, request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded)
