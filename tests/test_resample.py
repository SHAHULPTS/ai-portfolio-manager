"""Resampling and corrections, on tiny hand-made minute files with known answers."""

from pathlib import Path

import duckdb

from etl.resample_to_daily import daily_bars, source_files, symbol_for

HEADER = "date,open,high,low,close,volume\n"


def write(path: Path, rows: list[str]) -> Path:
    path.write_text(HEADER + "\n".join(rows) + "\n")
    return path


def bars(tmp_path, files, **corrections):
    con = duckdb.connect()
    rel = daily_bars(
        con, files, **{"listing_dates": {}, "bad_days": [], "demergers": [], **corrections}
    )
    return {(r[0], str(r[1])): r[2:] for r in rel.fetchall()}


def test_symbol_names():
    assert symbol_for(Path("TCS_minute.csv")) == "TCS"
    assert symbol_for(Path("NIFTY 50_minute.csv")) == "NIFTY50"
    assert symbol_for(Path("MM_minute_new.csv")) == "MM"


def test_duplicate_files_are_skipped(tmp_path):
    for name in ["MM_minute.csv", "MM_minute_new.csv", "LTIM_minute.csv", "LTM_minute.csv"]:
        write(tmp_path / name, [])
    assert sorted(source_files(tmp_path).values()) == ["LTM", "MM"]


def test_daily_ohlcv_from_minutes(tmp_path):
    f = write(
        tmp_path / "TCS_minute.csv",
        [
            "2024-01-01 09:15:00,100,102,99,101,10",
            "2024-01-01 09:16:00,101,105,100,104,20",
            "2024-01-01 15:29:00,104,104,98,99,30",
        ],
    )
    got = bars(tmp_path, {f: "TCS"})
    assert got[("TCS", "2024-01-01")] == (100, 105, 98, 99, 60)


def test_cleaning_rules(tmp_path):
    f = write(
        tmp_path / "TCS_minute.csv",
        [
            "2024-01-01 09:15:00,100,101,99,100,10",
            "2024-01-01 09:16:00,0,0,0,0,0",  # zero-filled gap: dropped
            "2024-01-01 15:29:00,100,103,99,102,10",
            "2024-01-01 16:00:00,500,500,500,500,1",  # after-close filler: dropped
            "2024-11-01 18:15:00,200,201,199,200,5",  # Muhurat session: kept
            "2024-11-01 18:16:00,200,199,200,203,5",  # high/low don't cover close: widened
        ],
    )
    got = bars(tmp_path, {f: "TCS"})
    assert got[("TCS", "2024-01-01")] == (100, 103, 99, 102, 20)
    assert got[("TCS", "2024-11-01")] == (200, 203, 199, 203, 10)


def test_corrections(tmp_path):
    f = write(
        tmp_path / "ABC_minute.csv",
        [
            "2024-01-01 15:29:00,10,10,10,10,1",  # before listing: dropped
            "2024-01-02 15:29:00,100,100,100,100,1",
            "2024-01-03 15:29:00,400,400,400,400,1",  # bad day: dropped
            "2024-01-04 15:29:00,100,100,100,100,1",
            "2024-01-05 15:29:00,60,60,60,60,1",  # demerger ex-date, factor 0.6
        ],
    )
    got = bars(
        tmp_path,
        {f: "ABC"},
        listing_dates={"ABC": "2024-01-02"},
        bad_days=[("ABC", "2024-01-03")],
        demergers=[("ABC", "2024-01-05")],
    )
    assert sorted(d for _, d in got) == ["2024-01-02", "2024-01-04", "2024-01-05"]
    assert got[("ABC", "2024-01-02")][3] == 60  # 100 scaled by 0.6
    assert got[("ABC", "2024-01-04")][3] == 60
    assert got[("ABC", "2024-01-05")][3] == 60  # ex-date itself unchanged


def test_demerger_adjustment_keeps_the_market_move(tmp_path):
    # Market falls 5% on the ex-date; the stock falls 43%. The adjusted stock should fall 5%.
    stock = write(
        tmp_path / "ABC_minute.csv",
        ["2024-01-04 15:29:00,100,100,100,100,1", "2024-01-05 15:29:00,57,57,57,57,1"],
    )
    index = write(
        tmp_path / "NIFTY 50_minute.csv",
        ["2024-01-04 15:29:00,200,200,200,200,0", "2024-01-05 15:29:00,190,190,190,190,0"],
    )
    got = bars(tmp_path, {stock: "ABC", index: "NIFTY50"}, demergers=[("ABC", "2024-01-05")])
    prev, ex = got[("ABC", "2024-01-04")][3], got[("ABC", "2024-01-05")][3]
    assert ex == 57
    assert round(ex / prev - 1, 4) == -0.05
