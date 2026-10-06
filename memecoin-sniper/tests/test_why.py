"""/why: what happened to recent launches, so 'no buys' is never a mystery."""
from sniper.models import Candidate
from tests.test_telegram import Harness


def _c(i, source="pumpfun"):
    return Candidate(chain="solana", mint=f"M{i}", source=source, symbol=f"T{i}")


async def test_reasons_are_counted_with_numbers_folded(tmp_path):
    h = Harness(tmp_path)
    e = h.eng
    e._note_outcome(_c(1), "❌ confirmation failed: only 3 early buyers (min 6); dev sold during "
                           "confirmation window")
    e._note_outcome(_c(2), "❌ confirmation failed: only 4 early buyers (min 6)")
    e._note_outcome(_c(3), "❌ rejected: dev bought 9.5% of supply at launch")
    e._note_outcome(_c(4), "🟢 BUY <b>X</b> 0.03 SOL")
    e._note_outcome(_c(5), "skipped: max open positions")
    e._note_outcome(_c(6, "manual"), "❌ rejected: whatever")   # user's own buys aren't counted
    text = e.why_summary()
    assert "5 launches looked at, 1 bought" in text
    assert "2 × only # early buyers (min #)" in text
    assert "1 × dev sold during confirmation window" in text
    assert "1 × dev bought #% of supply at launch" in text and "max open positions" in text
    assert "whatever" not in text
    await h.close()


async def test_nothing_seen_says_why(tmp_path):
    h = Harness(tmp_path)
    h.eng.paused = True
    assert "paused" in h.eng.why_summary()
    h.eng.paused = False
    assert "Health" in h.eng.why_summary()
    await h.close()


async def test_why_from_telegram(tmp_path):
    h = Harness(tmp_path)
    h.eng._note_outcome(_c(1), "❌ confirmation failed: too few buyers")
    await h.text("/why")
    assert "too few buyers" in h.last and "why" in h.buttons()
    await h.tap("st")
    assert "why" in h.buttons()
    await h.tap("why")
    assert "1 launches looked at" in h.last
    await h.close()
