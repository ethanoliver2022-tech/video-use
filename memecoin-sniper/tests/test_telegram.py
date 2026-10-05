import asyncio
import os
import stat

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from sniper.config import load_config
from sniper.engine import Engine
from sniper.execution.executors import LiveExecutor, PaperExecutor
from sniper.models import Fill, Position
from sniper.settings import BY_KEY, SETTINGS, parse_value
from sniper.telegram_bot import TelegramControl

OWNER = "4242"
MINT = str(Keypair().pubkey())


def idx(key):
    return next(i for i, s in enumerate(SETTINGS) if s.key == key)


class Harness:
    """Engine + Telegram UI with the network replaced by fakes."""

    def __init__(self, tmp_path, owner=OWNER):
        cfg = load_config("config.example.yaml")
        cfg.data_dir = str(tmp_path)
        cfg.filters.reject_reused_socials = False
        cfg.pumpportal_api_key = "test-key"
        self.eng = Engine(cfg, live=False, config_path="config.example.yaml", start_paused=True)
        self.eng.telegram_ui = True
        self.sent, self.api_calls, self.ws = [], [], []

        async def fake_ws(payload):
            self.ws.append(payload)
        self.eng.stream._send = fake_ws

        async def fake_telegram(text, buttons=None, chat_id=None):
            self.sent.append((text, buttons, chat_id))
        self.eng.notifier.telegram = fake_telegram

        async def balance(_):
            return 1.5
        self.eng.rpc.get_balance_sol = balance

        self.tg = TelegramControl(self.eng, "TOKEN", owner, self.eng.http)

        async def fake_api(method, **params):
            self.api_calls.append((method, params))
            if method == "editMessageText":
                raise RuntimeError("force fallback to send")
            return {"message_id": 777}
        self.tg.api = fake_api

    async def text(self, text, chat=OWNER, msg_id=1):
        await self.tg.handle_update({"message": {"chat": {"id": int(chat)}, "text": text,
                                                 "message_id": msg_id}})
        await self.eng.settle()

    async def tap(self, data, chat=OWNER, settle=True):
        await self.tg.handle_update({"callback_query": {"id": "q", "data": data,
                                                        "message": {"chat": {"id": int(chat)},
                                                                    "message_id": 5}}})
        if settle:
            await self.eng.settle()

    @property
    def last(self):
        return self.sent[-1][0]

    def buttons(self):
        return [d for row in (self.sent[-1][1] or []) for _, d in row]

    async def close(self):
        for t in list(self.eng._bg):
            t.cancel()
        await asyncio.gather(*self.eng._bg, return_exceptions=True)
        await self.eng.http.aclose()


# ---------- settings parsing ----------

def test_parse_values():
    assert parse_value(BY_KEY["exits.take_profit"], "50:50, 200%:30")[1].at_pct == 200
    with pytest.raises(ValueError):
        parse_value(BY_KEY["exits.take_profit"], "50:80,100:30")  # sells > 100%
    assert parse_value(BY_KEY["trading.buy_amount_sol"], "0.2 SOL") == 0.2
    with pytest.raises(ValueError):
        parse_value(BY_KEY["exits.stop_loss_pct"], "150")
    assert parse_value(BY_KEY["speed.jito_enabled"], "off") is False
    assert parse_value(BY_KEY["exits.kol_wallets"], "none") == []
    with pytest.raises(ValueError):
        parse_value(BY_KEY["exits.kol_wallets"], "notawallet")


# ---------- pairing ----------

async def test_pairing_with_code(tmp_path):
    h = Harness(tmp_path, owner="")
    code = h.tg.pair_code
    assert code
    await h.text("/start", chat="1")
    await h.text("/start WRONG", chat="1")
    assert h.tg.owner == "" and not h.sent
    await h.tg.handle_update({"message": {"chat": {"id": -100, "type": "group"},
                                          "text": f"/start {code}", "message_id": 3}})
    assert h.tg.owner == "" and "private chat" in h.sent[-1][0]  # never pairs a group
    h.sent.clear()
    await h.text(f"/start {code}", chat="1")
    assert h.tg.owner == "1" and "Paired" in h.sent[0][0]
    await h.text("/pause", chat="2")  # strangers are ignored after pairing too
    assert h.eng.store.get_setting("paused") is None
    # pairing survives restarts
    h2 = Harness(tmp_path, owner="")
    assert h2.tg.owner == "1" and not h2.tg.pair_code
    await h.close(); await h2.close()


# ---------- menu ----------

async def test_main_menu_and_start_stop(tmp_path):
    h = Harness(tmp_path)
    await h.text("/menu")
    assert "PAPER" in h.last and "No wallet yet" in h.last
    assert "go" in h.buttons() and "mode:live" in h.buttons()
    await h.tap("go")
    assert not h.eng.paused and "stop" in h.buttons()
    await h.tap("stop")
    assert h.eng.paused and h.eng.store.get_setting("paused") == "1"
    await h.close()


# ---------- wallet ----------

async def test_wallet_create_import_export(tmp_path):
    h = Harness(tmp_path)
    await h.tap("w:new")
    kp1 = h.eng.wallet.keypair()
    assert kp1 and str(kp1.pubkey()) in h.sent[-2][0]  # "New wallet created" + deposit address
    assert stat.S_IMODE(os.stat(h.eng.wallet.path).st_mode) == 0o600

    await h.tap("w:new")  # existing wallet -> confirmation, not replacement
    assert "w:new!" in h.buttons() and h.eng.wallet.keypair().pubkey() == kp1.pubkey()

    other = Keypair()
    await h.tap("w:imp")
    await h.text(str(other), msg_id=55)  # str(Keypair) is the base58 secret
    assert h.eng.wallet.keypair().pubkey() == other.pubkey()
    assert ("deleteMessage", {"chat_id": OWNER, "message_id": 55}) in h.api_calls
    backups = [p for p in os.listdir(tmp_path) if p.startswith("wallet.key.bak-")]
    assert len(backups) == 1  # old key kept, never deleted

    await h.tap("w:exp!", settle=False)  # don't wait out the 60s auto-delete
    method, params = h.api_calls[-1]
    assert method == "sendMessage" and h.eng.wallet.export() in params["text"]
    assert h.eng._bg  # auto-delete scheduled
    await h.close()


async def test_bad_key_keeps_prompt_open(tmp_path):
    h = Harness(tmp_path)
    await h.tap("w:imp")
    await h.text("garbage")
    assert "Try again" in h.last and h.tg.pending["kind"] == "import"
    await h.tap("x")
    assert h.tg.pending is None
    await h.close()


async def test_withdraw_flow(tmp_path):
    h = Harness(tmp_path)
    await h.tap("w:new")
    sent_txs = []

    async def blockhash():
        return Hash.new_unique()

    async def send_raw(raw):
        sent_txs.append(VersionedTransaction.from_bytes(raw))
        return "SIG"

    async def confirm(sig):
        return True
    h.eng.rpc.get_latest_blockhash, h.eng.rpc.send_raw_transaction, h.eng.rpc.confirm = \
        blockhash, send_raw, confirm

    dest = str(Keypair().pubkey())
    await h.tap("w:wd")
    await h.text(f"{dest} 0.5")
    assert "wd!" in h.buttons() and not sent_txs  # nothing sent before confirming
    await h.tap("wd!")
    assert "Sent 0.500000000 SOL" in h.last
    [tx] = sent_txs
    assert tx.verify_with_results() == [True]
    assert dest in [str(k) for k in tx.message.account_keys]

    await h.text(f"/withdraw {dest} 5")  # more than the 1.5 balance
    await h.tap("wd!")
    assert "balance is 1.5" in h.last
    await h.close()


# ---------- settings ----------

async def test_edit_settings_persist_and_presets(tmp_path):
    h = Harness(tmp_path)
    await h.tap(f"e:{idx('exits.stop_loss_pct')}")
    await h.text("500")
    assert "between" in h.last  # rejected, prompt still open
    await h.text("15")
    assert h.eng.cfg.exits.stop_loss_pct == 15

    jito = h.eng.cfg.speed.jito_enabled
    await h.tap(f"e:{idx('speed.jito_enabled')}")  # bools toggle on tap
    assert h.eng.cfg.speed.jito_enabled is (not jito)

    await h.tap(f"e:{idx('exits.take_profit')}")
    await h.text("50:50,200:50")
    assert [lvl.at_pct for lvl in h.eng.cfg.exits.take_profit] == [50, 200]

    await h.tap("pre:safe")
    assert h.eng.cfg.preset == "safe" and h.eng.cfg.entry.confirm_seconds == 6
    assert h.eng.cfg.exits.stop_loss_pct == 15  # custom setting still wins
    safety_cfg = h.eng.safety.cfg
    assert safety_cfg is h.eng.cfg.filters and safety_cfg.min_socials == 1  # updated in place
    await h.close()

    h2 = Harness(tmp_path)  # restart: overrides re-applied
    assert h2.eng.cfg.exits.stop_loss_pct == 15
    assert [lvl.at_pct for lvl in h2.eng.cfg.exits.take_profit] == [50, 200]
    await h2.tap("rst!")
    assert h2.eng.cfg.exits.stop_loss_pct == 25 and not h2.eng.store.overrides()
    await h2.close()


async def test_feed_toggle_resubscribes(tmp_path):
    h = Harness(tmp_path)
    await h.tap(f"e:{idx('discovery.pumpfun_migrations')}")
    assert {"method": "subscribeMigration"} in h.ws
    await h.tap(f"e:{idx('discovery.pumpfun_new_tokens')}")
    assert {"method": "unsubscribeNewToken"} in h.ws
    await h.close()


# ---------- mode switching ----------

async def test_go_live_requires_wallet_and_flat_book(tmp_path):
    h = Harness(tmp_path)
    await h.tap("mode:live")
    assert "Create or import a wallet first" in h.last
    await h.tap("w:new")
    await h.tap("mode:live")
    assert "mode:live!" in h.buttons() and not h.eng.live
    await h.tap("mode:live!")
    assert h.eng.live and isinstance(h.eng.executor, LiveExecutor)
    assert h.eng.store.get_setting("mode") == "live"
    await h.tap("w:new!")
    assert "Switch to PAPER" in h.last  # can't swap keys under a live executor

    h.eng.positions["X"] = Position(mint="X", symbol="X", source="manual", creator=None,
                                    entry_price=1, tokens_initial=1, tokens_remaining=1, sol_in=1)
    await h.tap("mode:paper")
    assert "Close your 1 open live position" in h.sent[-2][0] and h.eng.live
    h.eng.positions.clear()
    await h.tap("mode:paper")
    assert not h.eng.live and isinstance(h.eng.executor, PaperExecutor)
    await h.close()


# ---------- trading from chat ----------

async def test_paste_address_buy_card_and_copy_menu(tmp_path):
    h = Harness(tmp_path)

    async def fake_buy(c, sol, curve):
        return Fill(tokens=1000.0, sol=sol)
    h.eng.executor.buy = fake_buy
    # no network in tests: market data unavailable, and the token fails the filters
    h.eng.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    async def failing(c):
        from sniper.models import SafetyReport
        return SafetyReport(passed=False, reasons=["mint authority not revoked"])
    h.eng.safety.evaluate = failing

    async def no_curve(addr):
        return None
    h.eng.rpc.get_account_bytes = no_curve

    await h.text(MINT)
    assert "Fails your filters" in h.sent[-1][0]
    assert f"b:{MINT}:0.05" in h.buttons() and f"bf:{MINT}:" in h.buttons()
    await h.tap(f"bf:{MINT}:0.25")
    assert h.eng.positions[MINT].sol_in == 0.25 and "BUY" in h.sent[-1][0]

    await h.tap(f"bc:{MINT}")
    assert h.tg.pending["kind"] == "buy_custom"

    whale = str(Keypair().pubkey())
    await h.tap("c:add")
    await h.text(f"{whale} whale 0.1")
    assert h.eng.cfg.copytrade.enabled and whale in [w.address for w in h.eng.copy_wallets()]
    assert {"method": "subscribeAccountTrade", "keys": [whale]} in h.ws
    await h.tap(f"c:rm:{whale}")
    assert not h.eng.copy_wallets()
    await h.close()
