"""Backup copy watcher: copied wallets' swaps read straight from the chain."""
import time

from solders.keypair import Keypair

from sniper.copywatch import PUMP_PROGRAM, CopyPoller, trades_in
from sniper.models import SOL_MINT

W = str(Keypair().pubkey())
MINT = str(Keypair().pubkey()) + "pump"
MINT = MINT[-44:]


def _tx(sol_change=-0.5, tokens_before=0.0, tokens_after=1_000_000.0, logs=None, err=None,
        wsol=None):
    bal = lambda amt, mint=MINT: {"owner": W, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}  # noqa: E731
    pre = [bal(tokens_before)] if tokens_before else []
    post = [bal(tokens_after)] if tokens_after else []
    if wsol:
        pre.append(bal(wsol[0], SOL_MINT))
        post.append(bal(wsol[1], SOL_MINT))
    return {"transaction": {"message": {"accountKeys": [{"pubkey": W}, {"pubkey": "X"}]}},
            "meta": {"err": err, "preBalances": [2_000_000_000, 0],
                     "postBalances": [2_000_000_000 + int(sol_change * 1e9), 0],
                     "preTokenBalances": pre, "postTokenBalances": post,
                     "logMessages": logs or [f"Program {PUMP_PROGRAM} invoke [1]"]}}


def test_buys_and_sells_are_read_from_balance_changes():
    [buy] = trades_in(_tx(), W, "S1")
    assert (buy["txType"], buy["mint"], buy["pool"], buy["via"]) == ("buy", MINT, "pump", "rpc")
    assert buy["solAmount"] == 0.5 and buy["tokenAmount"] == 1_000_000
    [sell] = trades_in(_tx(sol_change=0.7, tokens_before=1_000_000, tokens_after=0,
                           logs=["Program JUP6 invoke [1]"]), W, "S2")
    assert sell["txType"] == "sell" and sell["pool"] == "other"
    # paid in wrapped SOL (Jupiter): still a buy for that much SOL
    [wbuy] = trades_in(_tx(sol_change=-0.002, wsol=(1.0, 0.5)), W, "S3")
    assert wbuy["txType"] == "buy" and abs(wbuy["solAmount"] - 0.502) < 1e-9
    assert trades_in(_tx(err={"x": 1}), W, "S4") == []          # failed tx
    assert trades_in(_tx(sol_change=-0.000005), W, "S5")[0]["txType"] == "buy"
    assert trades_in(_tx(sol_change=0.0001), W, "S6") == []     # tokens in, SOL in: airdrop-ish
    assert trades_in(_tx(), "someone else", "S7") == []


class FakeRpc:
    def __init__(self):
        self.sigs, self.txs, self.calls = [], {}, []

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            until = params[1].get("until")
            out = []
            for s in self.sigs:            # newest first
                if s["signature"] == until:
                    break
                out.append(s)
            return out
        return self.txs.get(params[0])


async def test_poller_starts_from_now_then_reports_new_swaps_once():
    rpc, seen = FakeRpc(), []

    async def on_trade(msg):
        seen.append(msg)
    rpc.sigs = [{"signature": "OLD", "blockTime": time.time() - 30, "err": None}]
    rpc.txs["OLD"] = _tx()
    p = CopyPoller(rpc, lambda: [W], on_trade)
    await p.poll_wallet(W)
    assert seen == []                                # history before watching is ignored
    rpc.sigs.insert(0, {"signature": "NEW", "blockTime": time.time(), "err": None})
    rpc.txs["NEW"] = _tx(sol_change=-0.2)
    await p.poll_wallet(W)
    await p.poll_wallet(W)
    assert [m["signature"] for m in seen] == ["NEW"] and seen[0]["solAmount"] == 0.2


async def test_engine_copies_from_the_chain_watcher_and_shows_it_in_the_menu(tmp_path, monkeypatch):
    from sniper import pump_curve
    import sniper.engine as engine_mod
    from sniper.models import Fill
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    eng = h.eng
    eng.paused = False
    await eng.add_copy_wallet(W, "whale", 0.02)

    async def buy(c, sol, curve):
        return Fill(tokens=1_000_000.0, sol=sol)
    eng.executor.buy = buy

    async def curve(rpc, mint):
        return pump_curve.CurveInfo(v_sol=32.0, v_tokens=1_073_000_000 * 30 / 32, complete=False)
    monkeypatch.setattr(engine_mod, "fetch_curve", curve)
    msg = trades_in(_tx(), W, "SIG")[0]
    await eng.copy_poller._enrich(msg)
    assert msg["marketCapSol"] > 0 and msg["vSolInBondingCurve"] == 32.0
    await eng.on_trade(msg)
    await eng.settle()
    assert MINT in eng.positions and eng.positions[MINT].sol_in == 0.02
    await eng.on_trade(dict(msg, via="pumpportal"))   # the same trade from PumpPortal: once
    await eng.settle()
    await h.tap("c")
    assert "via chain" in h.last and "chain backup ✅" in h.last
    await h.close()
