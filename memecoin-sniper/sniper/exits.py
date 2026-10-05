"""Exit rules — pure functions so they can be tested without a network.

Priority (first match wins):
  1. dev sold / copied wallet sold -> dump everything (the classic rug precursor)
  2. moonbag (if one is being kept) -> only its own wide trailing stop / max hold apply
  3. stop loss                     -> dump everything
  4. breakeven, trailing stop, sell pressure, max hold, stale
                                   -> sell everything, or down to the moonbag once
                                      profit has been taken
  5. KOL buy                       -> sell a chunk into the wave of followers they bring
  6. sell initials                 -> take your SOL back at a target gain
  7. take-profit ladder            -> scale out at fixed multiples
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import ExitConfig
from .models import Position, TradeTick, num

DUST_FRACTION = 0.02    # if a partial sell would leave < 2% of the original bag, sell it all
INITIALS_BUFFER = 1.03  # sell 3% extra when taking initials to cover fees and slippage


@dataclass
class ExitDecision:
    tokens: float
    sell_all: bool
    reason: str
    tp_index: Optional[int] = None
    kind: str = ""      # "tp" | "kol" | "initials" | "" (full or soft exit)


def record_trade(pos: Position, msg: dict, kol_wallets: set[str], own_wallet: str = "") -> None:
    trader = msg.get("traderPublicKey", "")
    side = msg.get("txType", "")
    if side not in ("buy", "sell") or (own_wallet and trader == own_wallet):
        return
    pos.recent_trades.append(TradeTick(side=side, trader=trader,
                                       sol=num(msg.get("solAmount"), allow_zero=True) or 0.0,
                                       ts=time.time()))
    if side == "sell" and pos.creator and trader == pos.creator:
        pos.dev_sold = True
    if side == "sell" and pos.leader and trader == pos.leader:
        pos.leader_sold = True
    if side == "buy" and trader in kol_wallets and trader not in pos.kol_bought:
        pos.kol_bought.append(trader)


def sell_pressure(pos: Position, cfg: ExitConfig) -> Optional[float]:
    window = list(pos.recent_trades)[-cfg.sell_pressure_window:]
    if len(window) < cfg.sell_pressure_min_trades:
        return None
    return sum(1 for t in window if t.side == "sell") / len(window)


def moonbag_size(pos: Position, cfg: ExitConfig) -> float:
    """Tokens to keep as a moonbag. Only once profit has been taken: a losing trade
    never leaves a bag behind."""
    if cfg.moonbag_pct <= 0 or not (pos.tp_levels_hit or pos.initials_taken):
        return 0.0
    return pos.tokens_initial * cfg.moonbag_pct / 100


def in_moonbag(pos: Position, cfg: ExitConfig) -> bool:
    bag = moonbag_size(pos, cfg)
    return bag > 0 and not pos.closed and pos.tokens_remaining <= bag * 1.0001


def evaluate(pos: Position, cfg: ExitConfig, now: Optional[float] = None) -> Optional[ExitDecision]:
    if pos.closed or pos.tokens_remaining <= 0:
        return None
    now = now or time.time()
    everything = lambda reason: ExitDecision(pos.tokens_remaining, True, reason)  # noqa: E731

    if cfg.exit_on_dev_sell and pos.dev_sold:
        return everything("dev sold")
    if pos.leader_sold:
        return everything("copied wallet sold")
    if cfg.sell_on_migration and pos.migrated and pos.seen_on_curve:
        return everything("migrated")  # an exit rule: retried like any other until it sells

    if in_moonbag(pos, cfg):
        if pos.drawdown_from_peak_pct >= cfg.moonbag_trailing_pct:
            return everything(f"moonbag trailing stop ({pos.drawdown_from_peak_pct:.0f}% off peak)")
        if now - pos.opened_at >= cfg.moonbag_max_hold_hours * 3600:
            return everything("moonbag max hold")
        return None

    if pos.pnl_pct <= -abs(cfg.stop_loss_pct):
        return everything(f"stop loss ({pos.pnl_pct:.0f}%)")

    bag = moonbag_size(pos, cfg)

    def soft(reason: str) -> ExitDecision:
        """Leave the trade, but keep the moonbag if profit has already been taken."""
        if bag <= 0:
            return everything(reason)
        return ExitDecision(max(0.0, pos.tokens_remaining - bag), False, reason)

    if cfg.breakeven_after_first_tp and pos.tp_levels_hit and pos.pnl_pct <= 0:
        return soft("breakeven stop")

    peak_gain = (pos.peak_price / pos.entry_price - 1) * 100 if pos.entry_price else 0
    if peak_gain >= cfg.trailing_activate_pct and pos.drawdown_from_peak_pct >= cfg.trailing_stop_pct:
        return soft(f"trailing stop ({pos.drawdown_from_peak_pct:.0f}% off peak +{peak_gain:.0f}%)")

    ratio = sell_pressure(pos, cfg)
    if ratio is not None and ratio >= cfg.sell_pressure_ratio:
        return soft(f"sell pressure ({ratio:.0%} of last trades are sells)")

    if now - pos.opened_at >= cfg.max_hold_seconds:
        return soft("max hold time")
    if now - pos.last_update >= cfg.stale_seconds:
        return soft("no price updates (dead token)")

    keep = pos.tokens_initial * cfg.moonbag_pct / 100 if cfg.moonbag_pct > 0 else 0.0

    if pos.kol_bought and not pos.kol_exit_done and cfg.kol_buy_sell_pct > 0:
        dec = _partial(pos, pos.tokens_remaining * cfg.kol_buy_sell_pct / 100,
                       f"KOL buy {pos.kol_bought[0][:6]}…", keep)
        if dec:
            dec.kind = "kol"
            return dec

    if (cfg.sell_initials_at_pct > 0 and not pos.initials_taken and pos.last_price > 0
            and pos.pnl_pct >= cfg.sell_initials_at_pct):
        dec = _partial(pos, pos.sol_in / pos.last_price * INITIALS_BUFFER,
                       f"sell initials at +{pos.pnl_pct:.0f}%", keep)
        if dec:
            dec.kind = "initials"
            return dec

    hit = [i for i, lvl in enumerate(cfg.take_profit)
           if pos.pnl_pct >= lvl.at_pct and i not in pos.tp_levels_hit]
    if hit:
        i = hit[-1]  # jump straight to the highest level crossed; lower ones are folded in
        pct = sum(cfg.take_profit[j].sell_pct for j in hit)
        dec = _partial(pos, pos.tokens_initial * pct / 100,
                       f"take profit +{cfg.take_profit[i].at_pct:.0f}%", keep)
        if dec is None:  # everything above the moonbag is already sold
            dec = ExitDecision(0.0, False, f"take profit +{cfg.take_profit[i].at_pct:.0f}%")
        dec.tp_index, dec.kind = i, "tp"
        return dec
    return None


def _partial(pos: Position, tokens: float, reason: str, keep: float = 0.0) -> Optional[ExitDecision]:
    """Sell `tokens`, but never into the moonbag (`keep`) and never leave dust."""
    sellable = max(0.0, pos.tokens_remaining - keep)
    tokens = min(tokens, sellable)
    if tokens <= 0:
        return None
    leftover = pos.tokens_remaining - tokens
    if leftover - keep <= pos.tokens_initial * DUST_FRACTION:
        if keep <= 0:
            return ExitDecision(pos.tokens_remaining, True, reason)
        tokens = sellable  # sell exactly down to the moonbag
    return ExitDecision(tokens, False, reason)


def apply_fill(pos: Position, dec: ExitDecision, tokens_sold: float, sol_received: float,
               cfg: ExitConfig) -> None:
    pos.tokens_remaining = max(0.0, pos.tokens_remaining - tokens_sold)
    pos.sol_out += sol_received
    if dec.tp_index is not None:
        pos.tp_levels_hit.update(range(dec.tp_index + 1))
    if dec.kind == "kol":
        pos.kol_exit_done = True
    if dec.kind == "initials":
        pos.initials_taken = True
    if dec.sell_all or pos.tokens_remaining <= pos.tokens_initial * 1e-6:
        pos.tokens_remaining = 0.0
        pos.closed = True
        pos.close_reason = dec.reason
