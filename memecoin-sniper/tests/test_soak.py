"""24/7 soak: days of launches, trades and housekeeping must not grow memory."""
import json
import random

from solders.keypair import Keypair

import tests.test_chaos as chaos


def sizes(eng):
    out = {k: len(v) for k, v in vars(eng).items()
           if isinstance(v, (dict, set, list)) or type(v).__name__ == "deque"}
    out.update({f"stream.{k}": len(v) for k, v in vars(eng.stream).items()
                if isinstance(v, (dict, set, list))})
    return out


async def test_days_of_traffic_keep_memory_flat(tmp_path, monkeypatch):
    rng = random.Random(7)
    eng = chaos.build(tmp_path, rng)
    eng.cfg.trading.max_open_positions = 5
    eng.cfg.entry.confirm_seconds = 0
    clock = [1_800_000_000.0]
    import sniper.engine as engine_mod
    monkeypatch.setattr(engine_mod.time, "time", lambda: clock[0])
    history = []
    for day in range(6):
        for i in range(4000):  # ~4000 launches a "day", each with some trades
            clock[0] += 86400 / 4000
            m = str(Keypair().pubkey())
            await eng.stream._dispatch(json.dumps({
                "txType": "create", "mint": m, "traderPublicKey": str(Keypair().pubkey()),
                "name": "S", "symbol": "S", "initialBuy": 1e6, "vSolInBondingCurve": 30,
                "vTokensInBondingCurve": 1.07e9, "signature": f"c{day}-{i}"}))
            for j in range(3):
                await eng.on_trade({"mint": m, "txType": "buy", "traderPublicKey": "t",
                                    "solAmount": 0.1, "vSolInBondingCurve": 31,
                                    "vTokensInBondingCurve": 1.05e9, "signature": f"t{day}-{i}-{j}"})
            while not eng.queue.empty():
                await eng.handle_candidate(eng.queue.get_nowait())
            if i % 50 == 0:  # the exit loop and housekeeping
                for p in list(eng.positions.values()):
                    if not p.closed:
                        p.dev_sold = True
                        eng._sell_next_try.pop(p.mint, None)
                        await eng.check_exit(p)
                await eng.settle()
            if i % 500 == 0:
                eng.prune()
        await eng.settle()
        eng.prune()
        history.append(sizes(eng))
    assert eng.store.events("buy"), "the soak never traded"
    first, last = history[1], history[-1]  # day 1 onward: caches have reached steady state
    grew = {k: (first.get(k), v) for k, v in last.items() if v > max(2 * first.get(k, 0), 50)}
    assert not grew, grew
    await eng.http.aclose()
