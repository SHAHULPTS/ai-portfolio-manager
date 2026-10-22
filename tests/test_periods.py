import pandas as pd
import pytest

from metrics.periods import resolve

AS_OF = pd.Timestamp("2026-04-08")
INCEPTION = pd.Timestamp("2018-03-13")


@pytest.mark.parametrize(
    "preset, start",
    [
        ("since_inception", "2018-03-13"),
        ("ytd", "2026-01-01"),
        ("mtd", "2026-04-01"),
        ("1m", "2026-03-09"),
        ("1y", "2025-04-09"),
        ("3y", "2023-04-09"),
    ],
)
def test_presets(preset, start):
    assert resolve(preset, AS_OF, INCEPTION) == (pd.Timestamp(start), AS_OF)


def test_period_is_clipped_to_inception():
    late = pd.Timestamp("2024-06-03")
    assert resolve("5y", AS_OF, late)[0] == late


def test_explicit_dates_win_and_end_is_clipped():
    assert resolve("ytd", AS_OF, INCEPTION, start="2020-01-01", end="2030-01-01") == (
        pd.Timestamp("2020-01-01"),
        AS_OF,
    )


def test_bad_inputs():
    with pytest.raises(ValueError, match="unknown period"):
        resolve("forever", AS_OF, INCEPTION)
    with pytest.raises(ValueError, match="after it ends"):
        resolve(None, AS_OF, INCEPTION, start="2025-01-01", end="2024-01-01")
