from config import load_settings


def test_defaults(monkeypatch):
    for name in ("RISK_FREE_RATE", "TRADING_DAYS", "SQL_ROW_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("config.load_dotenv", lambda: None)

    s = load_settings()

    assert s.risk_free_rate == 0.065
    assert s.trading_days == 252
    assert s.sql_row_limit == 500


def test_env_overrides(monkeypatch):
    monkeypatch.setattr("config.load_dotenv", lambda: None)
    monkeypatch.setenv("RISK_FREE_RATE", "0.07")
    monkeypatch.setenv("SQL_TIMEOUT_MS", "1000")

    s = load_settings()

    assert s.risk_free_rate == 0.07
    assert s.sql_timeout_ms == 1000
