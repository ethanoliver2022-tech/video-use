"""End to end: the real `sniper bot` process, against local fakes of Telegram, PumpPortal and
the Solana RPC. Pairs, starts sniping, buys a launch, exits on a dev dump, survives SIGTERM and
comes back paired with its books intact."""
import asyncio
import base64
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets
from solders.keypair import Keypair

ROOT = Path(__file__).resolve().parent.parent
CHAT = 4242


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHttp:
    """Telegram Bot API + Solana JSON-RPC on one local port."""

    def __init__(self):
        self.updates, self.sent, self.lock = [], [], threading.Lock()
        self.accounts: dict[str, bytes] = {}           # address -> account data
        self.balances: dict[tuple[str, str], int] = {}  # (owner, mint) -> raw amount
        self.next_update = 1
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or b"{}")
                reply = fake.handle(self.path, body)
                raw = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, path, body):
        if path.startswith("/bot"):
            method = path.rsplit("/", 1)[-1]
            if method == "getUpdates":
                with self.lock:
                    ups = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
                if not ups:
                    threading.Event().wait(0.2)  # a short long-poll
                return {"ok": True, "result": ups}
            if method in ("sendMessage", "editMessageText"):
                with self.lock:
                    self.sent.append(body.get("text", ""))
            if method == "getMe":
                return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "testbot"}}
            return {"ok": True, "result": {"message_id": len(self.sent) + 1} if method == "sendMessage"
                    else True}
        # Solana JSON-RPC
        method, params = body.get("method"), body.get("params") or []
        result = {"value": None}
        if method == "getAccountInfo" and params[0] in self.accounts:
            result = {"value": {"data": [base64.b64encode(self.accounts[params[0]]).decode(),
                                         "base64"]}}
        elif method == "getTokenAccountsByOwner":
            amount = self.balances.get((params[0], params[1]["mint"]))
            result = {"value": [] if amount is None else [{"account": {"data": {"parsed": {
                "info": {"tokenAmount": {"amount": str(amount), "decimals": 6}}}}}}]}
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}

    def set_curve(self, mint, creator, v_sol=30.0, v_tokens=1.07e9):
        """A pump.fun bonding-curve account in the real on-chain layout."""
        from solders.pubkey import Pubkey
        from sniper.pump_curve import bonding_curve_address
        data = bytes(8) + struct.pack("<QQQQQ?", int(v_tokens * 1e6), int(v_sol * 1e9),
                                      int((v_tokens - 280e6) * 1e6), 0, int(1e15), False)
        self.accounts[bonding_curve_address(mint)] = data + bytes(Pubkey.from_string(creator))

    def user(self, text=None, data=None):
        with self.lock:
            uid = self.next_update
            self.next_update += 1
            if data:
                self.updates.append({"update_id": uid, "callback_query": {
                    "id": str(uid), "data": data, "from": {"id": CHAT},
                    "message": {"chat": {"id": CHAT}, "message_id": 1}}})
            else:
                self.updates.append({"update_id": uid, "message": {
                    "message_id": uid, "chat": {"id": CHAT}, "from": {"id": CHAT}, "text": text}})

    def wait_sent(self, pattern, timeout=20.0, after=0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for t in self.sent[after:]:
                    if re.search(pattern, t):
                        return t
            threading.Event().wait(0.1)
        raise AssertionError(f"bot never sent {pattern!r}; sent: {self.sent[-8:]}")


class FakePumpPortal:
    def __init__(self):
        self.clients, self.subs = set(), []
        self.port = free_port()

    async def handler(self, ws):
        self.clients.add(ws)
        try:
            async for msg in ws:
                self.subs.append(json.loads(msg))
        finally:
            self.clients.discard(ws)

    async def push(self, msg):
        for ws in list(self.clients):
            await ws.send(json.dumps(msg))


async def run_e2e(tmp_path, stream_key=True):
    http, pp = FakeHttp(), FakePumpPortal()
    server = await websockets.serve(pp.handler, "127.0.0.1", pp.port)
    data = tmp_path / "data"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
preset: degen
data_dir: {data}
trading:
  cooldown_after_loss_seconds: 0   # the scenario buys again right after a loss
discovery:
  geckoterminal_networks: []
  dexscreener_profiles: false
entry:
  confirm_seconds: 0
filters:
  min_socials: 0
  honeypot_check: false
endpoints:
  rpc_url: http://127.0.0.1:{http.port}/
  jupiter_api: http://127.0.0.1:{http.port}/jup
  pumpportal_ws: ws://127.0.0.1:{pp.port}/api/data
""")
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": "123:T", "PUMPPORTAL_API_KEY": "k" if stream_key else "",
           "TELEGRAM_API_BASE": f"http://127.0.0.1:{http.port}", "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "SOLANA_PRIVATE_KEY": "", "TELEGRAM_CHAT_ID": ""}

    async def start():
        return await asyncio.create_subprocess_exec(
            sys.executable, "-m", "sniper", "--config", str(cfg), "bot", cwd=ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

    logs = []

    async def pump_logs(proc):
        async for line in proc.stdout:
            logs.append(line.decode(errors="replace"))

    proc = await start()
    reader = asyncio.create_task(pump_logs(proc))
    try:
        # 1. pairing: the code is only in the server log
        for _ in range(200):
            m = re.search(r"/start ([0-9A-F]{10})", "".join(logs))
            if m:
                break
            await asyncio.sleep(0.1)
        assert m, "".join(logs)
        http.user("/start WRONGCODE1")
        http.user(f"/start {m.group(1)}")  # sent the moment the code is shown: never dropped
        await asyncio.to_thread(http.wait_sent, r"Paired")
        # 2. start sniping
        http.user(data="go")
        await asyncio.to_thread(http.wait_sent, r"▶️ sniping")
        for _ in range(100):  # the stream connects and subscribes
            if any(s.get("method") == "subscribeNewToken" for s in pp.subs):
                break
            await asyncio.sleep(0.1)
        assert pp.subs, "".join(logs)
        # 3. a launch: the bot buys it
        mint, dev = str(Keypair().pubkey()), str(Keypair().pubkey())
        http.set_curve(mint, dev)
        http.balances[(dev, mint)] = int(1e6 * 1e6)  # the dev's initial buy, still held
        n = len(http.sent)
        await pp.push({"txType": "create", "mint": mint, "traderPublicKey": dev, "name": "Froggo",
                       "symbol": "TST", "initialBuy": 1e6, "vSolInBondingCurve": 30,
                       "vTokensInBondingCurve": 1.07e9, "marketCapSol": 28, "signature": "c1",
                       "uri": ""})
        await asyncio.to_thread(http.wait_sent, r"BUY.*TST", 20, n)
        # 4. the dev dumps: the bot exits (seen on the trade stream, or on-chain without one)
        n = len(http.sent)
        http.set_curve(mint, dev, v_sol=25, v_tokens=1.28e9)
        http.balances[(dev, mint)] = 0
        if stream_key:
            for _ in range(100):
                if any(s.get("method") == "subscribeTokenTrade" for s in pp.subs):
                    break
                await asyncio.sleep(0.1)
            await pp.push({"txType": "sell", "mint": mint, "traderPublicKey": dev, "solAmount": 2,
                           "vSolInBondingCurve": 25, "vTokensInBondingCurve": 1.28e9,
                           "signature": "s1"})
        await asyncio.to_thread(http.wait_sent, r"SELL.*TST.*dev sold", 20, n)
        for _ in range(100):  # the (billed) trade feed for a closed token is dropped
            if not stream_key or {"method": "unsubscribeTokenTrade", "keys": [mint]} in pp.subs:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(pp.subs)
        # 5. a second launch stays open across the restart
        mint2, dev2 = str(Keypair().pubkey()), str(Keypair().pubkey())
        http.set_curve(mint2, dev2)
        http.balances[(dev2, mint2)] = int(1e6 * 1e6)
        n = len(http.sent)
        await pp.push({"txType": "create", "mint": mint2, "traderPublicKey": dev2,
                       "name": "Keep", "symbol": "KEEP", "initialBuy": 1e6, "vSolInBondingCurve": 30,
                       "vTokensInBondingCurve": 1.07e9, "signature": "c2", "uri": ""})
        await asyncio.to_thread(http.wait_sent, r"BUY.*KEEP", 20, n)
        # 6. docker stop = SIGTERM
        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), 15)
        await reader
        # 7. restart: still paired (no new code), position restored, still sniping
        n_logs, n, n_subs = len(logs), len(http.sent), len(pp.subs)
        proc = await start()
        reader = asyncio.create_task(pump_logs(proc))
        await asyncio.to_thread(http.wait_sent, r"restored 1 open position", 20, n)
        await asyncio.to_thread(http.wait_sent, r"sniper starting", 20, n)
        assert not re.search(r"/start [0-9A-F]{10}", "".join(logs[n_logs:])), "lost the pairing"
        for _ in range(20):  # commands sent before it is listening are dropped on purpose
            http.user("/positions")
            try:
                await asyncio.to_thread(http.wait_sent, r"KEEP</b>[^|]*\nPnL", 1, n)
                break
            except AssertionError:
                pass
        else:
            raise AssertionError(http.sent[n:])
        for _ in range(100):  # the restored position's trade feed is back
            if not stream_key or any(s.get("method") == "subscribeTokenTrade" and mint2 in s.get("keys", [])
                   for s in pp.subs[n_subs:]):
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(pp.subs)
        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), 15)
        await reader
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        server.close()
        http.server.shutdown()
    text = "".join(logs)
    assert "Traceback" not in text, text[-3000:]
    assert not re.search(r"\bERROR\b", text), text[-3000:]
    return http


async def test_end_to_end_with_trade_stream(tmp_path):
    await run_e2e(tmp_path, stream_key=True)


async def test_end_to_end_free_mode_on_chain_polling(tmp_path):
    """No PumpPortal key (the default): prices and dev dumps come from the chain."""
    await run_e2e(tmp_path, stream_key=False)
