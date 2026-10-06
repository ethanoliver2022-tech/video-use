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


@pytest.fixture(autouse=True)
def _instant_paper_fills(monkeypatch):
    """Paper waits like a live fill (~1s); tests run thousands of paper trades."""
    import sniper.engine as engine
    monkeypatch.setattr(engine, "PAPER_BUY_DELAY", 0.0)
    monkeypatch.setattr(engine, "PAPER_SELL_DELAY", 0.0)
