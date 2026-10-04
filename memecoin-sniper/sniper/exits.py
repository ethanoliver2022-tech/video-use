"""Exit rules — pure functions so they can be tested without a network.

Priority (first match wins):
  1. dev sold            -> dump everything (the classic rug precursor)
  2. stop loss           -> dump everything
  3. trailing stop       -> dump everything once armed and price falls off the peak
  4. sell pressure       -> dump everything when most recent trades are sells
  5. max hold / stale    -> dump everything; memecoin edge decays in minutes
  6. KOL buy             -> sell a chunk into the wave of followers they bring
  7. take-profit ladder  -> scale out at fixed multiples
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import ExitConfig
from .models import Position, TradeTick

DUST_FRACTION = 0.02  # if a partial sell would leave < 2% of the original bag, sell it all


@dataclass
class ExitDecision:
    tokens: float
    sell_all: bool
    reason: str
    tp_index: Optional[int] = None


def record_trade(pos: Position, msg: dict, kol_wallets: set[str], own_wallet: str = "") -> None:
    trader = msg.get("traderPublicKey", "")
    side = msg.get("txType", "")
    if side not in ("buy", "sell") or (own_wallet and trader == own_wallet):
        return
    pos.recent_trades.append(TradeTick(side=side, trader=trader,
                                       sol=float(msg.get("solAmount") or 0), ts=time.time()))
    if side == "sell" and pos.creator and trader == pos.creator:
        pos.dev_sold = True
    if side == "buy" and trader in kol_wallets and trader not in pos.kol_bought:
        pos.kol_bought.append(trader)


def sell_pressure(pos: Position, cfg: ExitConfig) -> Optional[float]:
    window = list(pos.recent_trades)[-cfg.sell_pressure_window:]
    if len(window) < cfg.sell_pressure_min_trades:
        return None
    return sum(1 for t in window if t.side == "sell") / len(window)


def evaluate(pos: Position, cfg: ExitConfig, now: Optional[float] = None) -> Optional[ExitDecision]:
    if pos.closed or pos.tokens_remaining <= 0:
        return None
    now = now or time.time()
    everything = lambda reason: ExitDecision(pos.tokens_remaining, True, reason)  # noqa: E731

    if cfg.exit_on_dev_sell and pos.dev_sold:
        return everything("dev sold")

    if pos.pnl_pct <= -abs(cfg.stop_loss_pct):
        return everything(f"stop loss ({pos.pnl_pct:.0f}%)")

    peak_gain = (pos.peak_price / pos.entry_price - 1) * 100 if pos.entry_price else 0
    if peak_gain >= cfg.trailing_activate_pct and pos.drawdown_from_peak_pct >= cfg.trailing_stop_pct:
        return everything(f"trailing stop ({pos.drawdown_from_peak_pct:.0f}% off peak +{peak_gain:.0f}%)")

    ratio = sell_pressure(pos, cfg)
    if ratio is not None and ratio >= cfg.sell_pressure_ratio:
        return everything(f"sell pressure ({ratio:.0%} of last trades are sells)")

    if now - pos.opened_at >= cfg.max_hold_seconds:
        return everything("max hold time")
    if now - pos.last_update >= cfg.stale_seconds:
        return everything("no price updates (dead token)")

    if pos.kol_bought and not pos.kol_exit_done and cfg.kol_buy_sell_pct > 0:
        return _partial(pos, pos.tokens_remaining * cfg.kol_buy_sell_pct / 100,
                        f"KOL buy {pos.kol_bought[0][:6]}…")

    hit = [i for i, lvl in enumerate(cfg.take_profit)
           if pos.pnl_pct >= lvl.at_pct and i not in pos.tp_levels_hit]
    if hit:
        i = hit[-1]  # jump straight to the highest level crossed; lower ones are folded in
        pct = sum(cfg.take_profit[j].sell_pct for j in hit)
        dec = _partial(pos, pos.tokens_initial * pct / 100,
                       f"take profit +{cfg.take_profit[i].at_pct:.0f}%")
        dec.tp_index = i
        return dec
    return None


def _partial(pos: Position, tokens: float, reason: str) -> ExitDecision:
    tokens = min(tokens, pos.tokens_remaining)
    if pos.tokens_remaining - tokens <= pos.tokens_initial * DUST_FRACTION:
        return ExitDecision(pos.tokens_remaining, True, reason)
    return ExitDecision(tokens, False, reason)


def apply_fill(pos: Position, dec: ExitDecision, tokens_sold: float, sol_received: float,
               cfg: ExitConfig) -> None:
    pos.tokens_remaining = max(0.0, pos.tokens_remaining - tokens_sold)
    pos.sol_out += sol_received
    if dec.tp_index is not None:
        pos.tp_levels_hit.update(range(dec.tp_index + 1))
    if dec.reason.startswith("KOL"):
        pos.kol_exit_done = True
    if dec.sell_all or pos.tokens_remaining <= pos.tokens_initial * 1e-6:
        pos.tokens_remaining = 0.0
        pos.closed = True
        pos.close_reason = dec.reason
