"""Paper results must predict live ones: no fills off curves that aren't SOL bonding curves,
fills priced after the delay a live trade has, and a way to start paper results over."""
import asyncio
import struct

import pytest
from solders.keypair import Keypair

import sniper.engine as engine_mod
from sniper import pump_curve
from sniper.config import load_config
from sniper.execution.executors import CurveState
from sniper.models import Candidate, Fill
from sniper.safety import static_checks
from tests.test_telegram import Harness

STD_SOL, STD_TOK = 30.0, 1_073_000_000.0  # a fresh standard pump.fun curve


def test_standard_curves_are_recognised_anywhere_along_the_curve():
    assert pump_curve.is_standard(STD_SOL, STD_TOK)
    k = STD_SOL * STD_TOK
    assert pump_curve.is_standard(85.0, k / 85.0)          # near graduation
    assert not pump_curve.is_standard(4000.0, STD_TOK)     # e.g. reserves counted in USDC
    assert not pump_curve.is_standard(4.0, STD_TOK)        # USDC raw read as lamports
    assert not pump_curve.is_standard(0, 0)


def test_a_coin_that_isnt_a_standard_sol_curve_isnt_bought():
    cfg = load_config("config.example.yaml")
    c = Candidate(chain="solana", mint="M", source="pumpfun", v_sol=4000.0, v_tokens=STD_TOK)
    r = static_checks(c, cfg.filters)
    assert not r.passed and any("standard SOL" in x for x in r.reasons)
    ok = Candidate(chain="solana", mint="M", source="pumpfun", v_sol=STD_SOL, v_tokens=STD_TOK)
    assert static_checks(ok, cfg.filters).passed


def _curve(quote: bytes = bytes(32), v_sol=30_000_000_000, v_tok=1_073_000_000_000_000):
    return (bytes(8) + struct.pack("<QQQQQ?", v_tok, v_sol, 793_100_000_000_000, 0,
                                   1_000_000_000_000_000, False)
            + bytes(Keypair().pubkey()) + b"\x00\x00" + quote)


def test_curve_quote_coin_is_read_from_the_chain():
    assert pump_curve.parse_curve(_curve()).sol_quoted                         # unset: SOL
    assert pump_curve.parse_curve(_curve(pump_curve.SOL_MINT_BYTES)).sol_quoted
    assert not pump_curve.parse_curve(_curve(bytes(Keypair().pubkey()))).sol_quoted
    assert pump_curve.parse_curve(_curve()[:81]).sol_quoted                    # older layout


async def test_a_non_sol_curve_never_prices_a_fill_or_an_exit(tmp_path):
    h = Harness(tmp_path)
    eng = h.eng
    eng._set_curve("A", CurveState(STD_SOL, STD_TOK))
    assert "A" in eng.curves
    eng._set_curve("A", CurveState(4000.0, STD_TOK))   # e.g. a PumpSwap pool or USDC coin
    assert "A" not in eng.curves                       # forgotten, never used to price

    async def usdc_curve(rpc, mint):
        return pump_curve.CurveInfo(v_sol=4.0, v_tokens=STD_TOK, complete=False,
                                    sol_quoted=False)
    engine_mod_fetch = engine_mod.fetch_curve
    engine_mod.fetch_curve = usdc_curve
    try:
        with pytest.raises(ValueError, match="standard SOL"):
            await eng._paper_curve(Candidate(chain="solana", mint="B", source="pumpfun"))
    finally:
        engine_mod.fetch_curve = engine_mod_fetch
    await h.close()


async def test_paper_buys_are_priced_after_a_live_like_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "PAPER_BUY_DELAY", 0.05)
    h = Harness(tmp_path)
    eng = h.eng
    eng.paused = False
    mint = str(Keypair().pubkey()) + "pump"
    mint = mint[-44:]
    eng._set_curve(mint, CurveState(STD_SOL, STD_TOK))
    priced = []

    async def buy(c, sol, curve):
        priced.append(curve.v_sol)
        return Fill(tokens=curve.buy_out(sol), sol=sol)
    eng.executor.buy = buy

    async def snipers_buy_meanwhile():
        await asyncio.sleep(0.02)  # other bots buy in the second before a live fill
        eng._set_curve(mint, CurveState(32.0, STD_SOL * STD_TOK / 32.0))
    c = Candidate(chain="solana", mint=mint, source="pumpfun", symbol="T", route="pump",
                  force=True)
    await asyncio.gather(eng.try_buy(c), snipers_buy_meanwhile())
    assert priced == [32.0]                    # the price after the delay, not at decision
    await h.close()


async def test_paper_results_can_start_over_and_live_ones_cannot(tmp_path):
    h = Harness(tmp_path)
    st = h.eng.store
    st.event("close", "M", "X", pnl_sol=26.9)
    st.event("buy", "M", "X", sol=0.01)
    st.event("block", "M", "X")               # not a result: kept
    st.mode = "live"
    st.event("close", "L", "Y", pnl_sol=-0.002)
    st.mode = "paper"
    await h.tap("st")
    assert "stz" in h.buttons()
    await h.tap("stz")
    assert "stz!" in h.buttons() and "paper" in h.last.lower()
    await h.tap("stz!")
    assert "cleared (2 records)" in h.last
    assert not st.events("close") and st.events("block")
    st.mode = "live"
    assert len(st.events("close")) == 1       # live untouched
    with pytest.raises(ValueError):
        st.clear_results("live")
    await h.close()
