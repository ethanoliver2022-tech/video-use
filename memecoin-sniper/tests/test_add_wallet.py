"""Adding copy / track wallets: paste an address or link, then tap through."""
from solders.keypair import Keypair

from sniper.telegram_bot import wallet_addresses
from tests.test_telegram import Harness

W1, W2 = str(Keypair().pubkey()), str(Keypair().pubkey())


def test_addresses_come_out_of_links_and_lists():
    assert wallet_addresses(f"https://solscan.io/account/{W1}") == [W1]
    assert wallet_addresses(f"https://gmgn.ai/sol/address/{W1}?tab=activity\n{W2}\n{W1}") == [W1, W2]
    assert wallet_addresses("hello there") == []


async def test_add_one_wallet_by_tapping(tmp_path):
    h = Harness(tmp_path)
    await h.tap("c:add")
    await h.text(f"https://gmgn.ai/sol/address/{W1}")
    assert "cw:m:copy" in h.buttons() and "cw:m:alert" in h.buttons()
    await h.tap("cw:m:copy")
    assert "cw:s:0.03" in h.buttons() and "cw:s:?" in h.buttons()
    await h.tap("cw:s:?")
    await h.text("0.02")
    assert "cw:n:skip" in h.buttons()
    await h.text("whale 1")
    w = next(x for x in h.eng.store.copy_wallets() if x["address"] == W1)
    assert (w["label"], w["buy_sol"], w["mode"]) == ("whale 1", 0.02, "copy")
    assert any("Copying whale 1 · 0.02 SOL per buy" in t for t, *_ in h.sent)
    await h.close()


async def test_track_only_and_several_at_once(tmp_path):
    h = Harness(tmp_path)
    await h.tap("c:add")
    await h.text(f"{W1}\n{W2}")
    assert "2 wallets" in h.last
    await h.tap("cw:m:alert")
    modes = {x["address"]: x["mode"] for x in h.eng.store.copy_wallets()}
    assert modes == {W1: "alert", W2: "alert"}
    await h.close()


async def test_the_old_one_line_form_still_works(tmp_path):
    h = Harness(tmp_path)
    await h.tap("c:add")
    await h.text(f"{W1} bigwhale 0.05")
    w = next(x for x in h.eng.store.copy_wallets() if x["address"] == W1)
    assert (w["label"], w["buy_sol"]) == ("bigwhale", 0.05)
    await h.close()


async def test_pasting_a_wallet_anywhere_offers_to_copy_it(tmp_path):
    h = Harness(tmp_path)

    async def accounts(addrs):
        return [("11111111111111111111111111111111", b"")]
    h.eng.rpc.get_accounts_raw = accounts
    await h.text(W1)
    assert "What should I do with it?" in h.last and "cw:m:copy" in h.buttons()
    await h.tap("cw:m:alert")
    await h.tap("cw:n:skip")
    assert any(x["address"] == W1 and x["mode"] == "alert" for x in h.eng.store.copy_wallets())
    await h.close()
