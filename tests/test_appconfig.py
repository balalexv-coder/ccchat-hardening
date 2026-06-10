"""Unit tests for the persisted reaper config (appconfig)."""
import importlib


def _fresh(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CCCHAT_APP_CONFIG", str(tmp_path / "app-config.json"))
    import backend.appconfig as ac
    return importlib.reload(ac)


def test_defaults_off_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CCCHAT_IDLE_REAP_HOURS", raising=False)
    ac = _fresh(tmp_path, monkeypatch)
    r = ac.get_reap()
    assert r["enabled"] is False and r["hours"] == 6.0 and r["interval_min"] == 30.0


def test_env_seeds_enabled(tmp_path, monkeypatch):
    ac = _fresh(tmp_path, monkeypatch, CCCHAT_IDLE_REAP_HOURS="8", CCCHAT_IDLE_REAP_INTERVAL_MIN="15")
    r = ac.get_reap()
    assert r["enabled"] is True and r["hours"] == 8.0 and r["interval_min"] == 15.0


def test_set_persists_and_sanitises(tmp_path, monkeypatch):
    ac = _fresh(tmp_path, monkeypatch)
    ac.set_reap(enabled=True, hours=0.0, interval_min=0)      # clamped to mins
    r = ac.get_reap()
    assert r["enabled"] is True and r["hours"] == 0.25 and r["interval_min"] == 1.0
    # survives a reload (persisted to disk)
    import importlib, backend.appconfig as ac2
    ac2 = importlib.reload(ac2)
    assert ac2.get_reap()["enabled"] is True
