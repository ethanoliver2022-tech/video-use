"""A small PnL chart for an open position, drawn as a PNG with the standard library only
(zlib + struct), so the Docker image needs no imaging packages.

The chart shows profit/loss % since the buy over time, with the entry (0%), the stop loss
and the take-profit levels as dashed lines. The numbers themselves go in the caption."""
from __future__ import annotations

import struct
import zlib
from typing import Iterable, Optional, Sequence

W, H = 640, 360
LEFT, RIGHT, TOP, BOTTOM = 62, 14, 14, 30

BG = (18, 22, 30)
GRID = (40, 46, 58)
AXIS = (120, 128, 140)
UP = (46, 204, 113)
DOWN = (231, 76, 60)
TP = (46, 160, 90)
SL = (190, 60, 50)
ENTRY = (150, 150, 160)

# 3x5 bitmap font for axis labels: each glyph is five rows of three bits
FONT = {
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
    "+": ("000", "010", "111", "010", "000"), "-": ("000", "000", "111", "000", "000"),
    "%": ("101", "001", "010", "100", "101"), ".": ("000", "000", "000", "000", "010"),
    "m": ("000", "000", "111", "111", "101"), "s": ("000", "011", "010", "001", "110"),
    "d": ("001", "001", "111", "101", "111"), "h": ("100", "100", "111", "101", "101"),
    " ": ("000", "000", "000", "000", "000"),
}


class Canvas:
    def __init__(self, w: int, h: int, bg=BG):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def dot(self, x: int, y: int, c) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def hline(self, y: int, x0: int, x1: int, c, dash: int = 0) -> None:
        for x in range(x0, x1 + 1):
            if not dash or (x // dash) % 2 == 0:
                self.dot(x, y, c)

    def vline(self, x: int, y0: int, y1: int, c) -> None:
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.dot(x, y, c)

    def line(self, x0: int, y0: int, x1: int, y1: int, c, width: int = 2) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for i in range(steps + 1):
            x = round(x0 + (x1 - x0) * i / steps)
            y = round(y0 + (y1 - y0) * i / steps)
            for dx in range(width):
                for dy in range(width):
                    self.dot(x + dx, y + dy, c)

    def text(self, x: int, y: int, s: str, c, scale: int = 2) -> None:
        for ch in s:
            glyph = FONT.get(ch)
            if glyph:
                for row, bits in enumerate(glyph):
                    for col, bit in enumerate(bits):
                        if bit == "1":
                            for dx in range(scale):
                                for dy in range(scale):
                                    self.dot(x + col * scale + dx, y + row * scale + dy, c)
            x += 4 * scale

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _label(pct: float) -> str:
    return f"{pct:+.0f}%"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m" if seconds >= 120 else f"{seconds}s"


def render(points: Sequence[tuple[float, float]], entry_price: float,
           stop_loss_pct: Optional[float] = None,
           take_profits: Iterable[float] = ()) -> bytes:
    """`points` are (unix time, price). Returns PNG bytes of PnL % over time."""
    c = Canvas(W, H)
    pts = [(t, (p / entry_price - 1.0) * 100.0) for t, p in points
           if entry_price > 0 and p > 0]
    if not pts:
        return c.png()
    if len(pts) == 1:
        pts.append((pts[0][0] + 1, pts[0][1]))
    t0, t1 = pts[0][0], pts[-1][0]
    if t1 <= t0:
        t1 = t0 + 1
    lo = min(v for _, v in pts)
    hi = max(v for _, v in pts)
    levels: list[tuple[float, tuple]] = [(0.0, ENTRY)]
    if stop_loss_pct:
        levels.append((-abs(stop_loss_pct), SL))
        lo = min(lo, -abs(stop_loss_pct))
    tps = sorted(take_profits)
    nxt = next((tp for tp in tps if tp > hi), None)
    if nxt is not None:
        hi = max(hi, nxt)       # always show the next target so you can see how far off it is
    levels += [(tp, TP) for tp in tps if tp <= hi]
    lo, hi = min(lo, 0.0), max(hi, 0.0)
    pad = max((hi - lo) * 0.08, 2.0)
    lo, hi = lo - pad, hi + pad

    def X(t: float) -> int:
        return LEFT + round((t - t0) / (t1 - t0) * (W - LEFT - RIGHT))

    def Y(v: float) -> int:
        return TOP + round((hi - v) / (hi - lo) * (H - TOP - BOTTOM))

    # frame, levels with their labels on the left
    c.vline(LEFT - 1, TOP, H - BOTTOM, AXIS)
    c.hline(H - BOTTOM, LEFT - 1, W - RIGHT, AXIS)
    used: list[int] = []
    for v, colour in sorted(levels, key=lambda lv: lv[1] is ENTRY):
        y = Y(v)
        c.hline(y, LEFT, W - RIGHT, colour if colour is not ENTRY else GRID, dash=0 if colour is ENTRY else 6)
        if all(abs(y - u) > 12 for u in used):
            text = _label(v)
            c.text(LEFT - 6 - len(text) * 8, y - 5, text, colour if colour is not ENTRY else AXIS)
            used.append(y)
    # time axis: start and elapsed
    c.text(LEFT, H - BOTTOM + 8, "0s", AXIS)
    span = _duration(t1 - t0)
    c.text(W - RIGHT - len(span) * 8, H - BOTTOM + 8, span, AXIS)
    # the PnL line, coloured by where it is now
    colour = UP if pts[-1][1] >= 0 else DOWN
    prev = None
    for t, v in pts:
        cur = (X(t), Y(v))
        if prev:
            c.line(prev[0], prev[1], cur[0], cur[1], colour)
        prev = cur
    # a marker on the latest price
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            c.dot(prev[0] + dx, prev[1] + dy, colour)
    return c.png()


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 2 * 86400:
        return f"{seconds // 86400}d"
    if seconds >= 2 * 3600:
        return f"{seconds // 3600}h"
    return _duration(seconds)


def render_pnl(points: Sequence[tuple[float, float]]) -> bytes:
    """Running profit (SOL) over time: a step line, green above zero and red below, with
    the zero line and the low / high / final values labelled."""
    c = Canvas(W, H)
    if len(points) < 2:
        return c.png()
    t0, t1 = points[0][0], points[-1][0]
    if t1 <= t0:
        t1 = t0 + 1
    vals = [v for _, v in points]
    lo, hi = min(min(vals), 0.0), max(max(vals), 0.0)
    pad = max((hi - lo) * 0.1, 0.002)
    lo, hi = lo - pad, hi + pad

    def X(t: float) -> int:
        return LEFT + round((t - t0) / (t1 - t0) * (W - LEFT - RIGHT))

    def Y(v: float) -> int:
        return TOP + round((hi - v) / (hi - lo) * (H - TOP - BOTTOM))

    c.vline(LEFT - 1, TOP, H - BOTTOM, AXIS)
    c.hline(H - BOTTOM, LEFT - 1, W - RIGHT, AXIS)
    c.hline(Y(0.0), LEFT, W - RIGHT, ENTRY, dash=6)
    used: list[int] = []
    for v, colour in ((0.0, AXIS), (max(vals), UP), (min(vals), DOWN), (vals[-1], AXIS)):
        y = Y(v)
        if all(abs(y - u) > 12 for u in used):
            text = f"{v:+.3f}" if abs(v) < 10 else f"{v:+.1f}"
            c.text(LEFT - 6 - len(text) * 8, y - 5, text, colour)
            used.append(y)
    c.text(LEFT, H - BOTTOM + 8, "-" + _span(t1 - t0), AXIS)   # how long ago the chart starts
    c.text(W - RIGHT - 8, H - BOTTOM + 8, "0", AXIS)
    prev = None
    for t, v in points:   # steps: the total only changes when a trade closes
        x, y = X(t), Y(v)
        if prev:
            colour = UP if prev[2] >= 0 else DOWN
            c.line(prev[0], prev[1], x, prev[1], colour)
            c.line(x, prev[1], x, y, UP if v >= 0 else DOWN)
        prev = (x, y, v)
    return c.png()
