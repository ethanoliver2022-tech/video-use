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


def test_why_groups_wallet_tags_and_shows_the_closest_calls(tmp_path):
    from sniper.models import Candidate
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    e = h.eng
    c = Candidate(chain="solana", mint="M", source="pumpfun")
    e._note_outcome(c, "❌ confirmation failed: one wallet is 51% of early buys (BwWK7f…)")
    e._note_outcome(c, "❌ confirmation failed: one wallet is 40% of early buys (9xQeWv…)")
    e._note_outcome(c, "❌ confirmation failed: only 3 unique buyers (&lt; 6); not enough net buying")
    e._note_outcome(c, "❌ dev check: dev wallet is only 12 min old")
    e._note_outcome(c, "❌ rejected: dev bought 9.1% of supply at launch")
    text = e.why_summary()
    assert "2 × one wallet is #% of early buys\n" in text + "\n"
    near = text.split("Closest calls")[1]
    assert "one wallet is" in near and "dev wallet is only # min old" in near
    assert "unique buyers" not in near and "dev bought" not in near


async def test_why_counts_can_be_reset(tmp_path):
    from sniper.models import Candidate
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    e = h.eng
    c = Candidate(chain="solana", mint="M", source="pumpfun")
    e._note_outcome(c, "❌ rejected: dev bought 9.1% of supply at launch")
    await h.tap("why")
    assert "1 launches" in h.last and "whyz" in h.buttons()
    await h.tap("whyz")
    assert "reset" in h.last
    await h.tap("why")
    assert "Since reset" in h.last and "no launches handled yet" in h.last
    e._note_outcome(c, "❌ confirmation failed: only 2 unique buyers (&lt; 6)")
    await h.tap("why")
    assert "Since reset (0 min ago)</b>: 1 launches" in h.last and "dev bought" not in h.last
    await h.close()


def test_socials_reused_counts_as_one_reason():
    from sniper.engine import Engine
    k = Engine._reason_key
    assert k("socials reused from an earlier launch: twitter.com/a/status/1, world.org") \
        == k("socials reused from an earlier launch: twitter.com/b/status/2") \
        == "socials reused from an earlier launch"
