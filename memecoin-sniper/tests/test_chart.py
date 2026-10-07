"""Live chart for an open position: a PNG drawn without imaging packages, updated in place
while switched on, and stopped by a button, by closing the position or after a time limit."""
import asyncio
import struct
import time
import zlib

from solders.keypair import Keypair

from sniper.chart import render
from sniper.models import Fill, Position
from tests.test_telegram import Harness


def _png_size(png: bytes):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    # the image data must decompress to exactly one filter byte + RGB row per line
    i, idat = 8, b""
    while i < len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        kind = png[i + 4:i + 8]
        if kind == b"IDAT":
            idat += png[i + 8:i + 8 + n]
        i += 12 + n
    assert len(zlib.decompress(idat)) == h * (1 + w * 3)
    return w, h


def test_the_chart_is_a_valid_png_for_any_history():
    now = time.time()
    pts = [(now + i, 1e-8 * (1 + 0.1 * (i % 7) - 0.2 * (i % 3))) for i in range(300)]
    assert _png_size(render(pts, 1e-8, 15, [40, 100, 250])) == (640, 360)
    assert _png_size(render([(now, 1e-8)], 1e-8, 15, [40]))          # just bought
    assert _png_size(render([], 1e-8, None, []))                      # nothing yet
    assert _png_size(render([(now, 2e-8), (now, 3e-8)], 1e-8))        # same timestamp


def test_positions_record_a_price_history_that_is_never_saved():
    p = Position(mint="M", symbol="X", source="t", creator=None, entry_price=1.0,
                 tokens_initial=10, tokens_remaining=10, sol_in=1, opened_at=100.0)
    assert list(p.price_history) == [(100.0, 1.0)]
    p.update_price(1.2, ts=100.2)          # same second: replaces the point
    p.update_price(1.5, ts=102.0)
    p.update_price(float("nan"), ts=103.0)
    assert list(p.price_history) == [(100.0, 1.2), (102.0, 1.5)]
    d = p.to_dict()
    assert "price_history" not in d
    assert len(Position.from_dict(d).price_history) == 1


async def _open_position(h):
    async def buy(c, sol, curve):
        return Fill(tokens=1_000_000.0, sol=sol)
    h.eng.executor.buy = buy
    res = await h.eng.manual_buy(str(Keypair().pubkey()), 0.03, force=True)
    assert res.startswith("🟢"), res
    return next(iter(h.eng.positions.values()))


async def test_live_chart_updates_and_stops_with_its_button(tmp_path):
    h = Harness(tmp_path)
    pos = await _open_position(h)
    uploads = []

    async def api_files(method, files, **params):
        assert files["photo"][1][:4] == b"\x89PNG"
        uploads.append((method, params))
        return {"message_id": 900}
    h.tg.api_files = api_files
    h.tg.CHART_EVERY = 0.01

    await h.tap("p")
    assert f"ch:{pos.mint}" in h.buttons()
    await h.tap(f"ch:{pos.mint}")
    assert uploads[0][0] == "sendPhoto" and pos.mint in h.tg.charts
    pos.update_price(pos.entry_price * 1.5)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if any(m == "editMessageMedia" for m, _ in uploads):
            break
    edit = [p for m, p in uploads if m == "editMessageMedia"][0]
    assert "+50" in edit["media"]["caption"] and edit["message_id"] == 900
    n = len(uploads)
    await asyncio.sleep(0.05)
    assert len(uploads) == n                       # nothing changed: no edits sent

    task = h.tg.charts[pos.mint]
    await h.tap(f"chx:{pos.mint}")
    await asyncio.gather(task, return_exceptions=True)
    assert pos.mint not in h.tg.charts
    final = uploads[-1][1]
    assert "stopped" in final["media"]["caption"]
    assert f"ch:{pos.mint}" in str(final["reply_markup"])  # restart button
    await h.tap(f"chx:{pos.mint}")
    assert "already stopped" in h.last
    await h.close()


async def test_live_chart_ends_when_the_position_closes(tmp_path):
    h = Harness(tmp_path)
    pos = await _open_position(h)
    uploads = []

    async def api_files(method, files, **params):
        uploads.append((method, params))
        return {"message_id": 901}
    h.tg.api_files = api_files
    h.tg.CHART_EVERY = 0.01
    await h.tap(f"ch:{pos.mint}")
    task = h.tg.charts[pos.mint]
    pos.closed, pos.close_reason = True, "take profit"
    await asyncio.wait_for(task, 2)
    assert "position closed" in uploads[-1][1]["media"]["caption"]
    assert pos.mint not in h.tg.charts
    await h.tap(f"ch:{pos.mint}")
    assert "closed" in h.last
    await h.close()


async def test_one_chart_runs_at_a_time(tmp_path):
    h = Harness(tmp_path)

    async def api_files(method, files, **params):
        return {"message_id": 902}
    h.tg.api_files = api_files
    h.tg.CHART_EVERY = 10
    for _ in range(3):
        await _open_position(h)
    opened = [m for m in h.eng.positions]
    for m in opened:
        await h.tap(f"ch:{m}")
    assert list(h.tg.charts) == [opened[-1]]     # opening a new chart stops the older one
    for m in list(h.tg.charts):
        h.tg.stop_chart(m)
    await asyncio.sleep(0)
    await h.close()
