"""Only the owner can reach funds, keys and data."""
import os
import stat

import pytest
from solders.keypair import Keypair

from sniper.config import load_config
from sniper.settings import BY_KEY


async def test_withdraw_allowlist_blocks_any_other_address(tmp_path, monkeypatch):
    from tests.test_review_fixes import engine
    mine = str(Keypair().pubkey())
    monkeypatch.setenv("WITHDRAW_ALLOWLIST", mine)
    eng = engine(tmp_path)
    eng.cfg.withdraw_allowlist = load_config("config.example.yaml").withdraw_allowlist
    assert eng.cfg.withdraw_allowlist == [mine]
    eng.wallet.create()

    async def bal(*a):
        return 1.0
    eng.rpc.get_balance_sol = bal
    with pytest.raises(ValueError, match="WITHDRAW_ALLOWLIST"):
        await eng.withdraw(str(Keypair().pubkey()), 0.1)  # an attacker's address
    await eng.http.aclose()


async def test_key_export_can_be_disabled(tmp_path):
    from tests.test_telegram import Harness
    h = Harness(tmp_path)
    await h.tap("w:new")
    h.eng.cfg.allow_key_export = False
    await h.tap("w:exp!", settle=False)
    secret = h.eng.wallet.export()
    assert all(secret not in str(c) for c in h.api_calls)
    assert all(secret not in t for t, *_ in h.sent) and "turned off" in h.last
    await h.close()


def test_env_locks_cannot_be_changed_from_telegram():
    assert not any(k.endswith(("withdraw_allowlist", "allow_key_export", "private_key"))
                   for k in BY_KEY)


def test_env_lock_parsing(monkeypatch):
    monkeypatch.setenv("ALLOW_KEY_EXPORT", "false")
    monkeypatch.setenv("WITHDRAW_ALLOWLIST", " A , B ,")
    cfg = load_config(None)
    assert cfg.allow_key_export is False and cfg.withdraw_allowlist == ["A", "B"]
    monkeypatch.delenv("ALLOW_KEY_EXPORT")
    assert load_config(None).allow_key_export is True


def test_data_folder_and_files_are_owner_only(tmp_path):
    from sniper.execution.wallet import WalletManager
    from sniper.store import Store
    old = os.umask(0o077)  # what the app sets at startup
    try:
        d = tmp_path / "data"
        Store(str(d)).event("x")
        WalletManager(str(d)).create()
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        for f in d.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, f
    finally:
        os.umask(old)


def test_startup_sets_a_private_umask():
    import inspect
    import sniper.__main__ as m
    assert "os.umask(0o077)" in inspect.getsource(m.main)
