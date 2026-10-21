"""Simulate one portfolio's trading life on real prices. Pure: no database, no global state.

The life cycle, evaluated on each trading day from inception:
1. Forced exit: a stock whose data ends before the dataset does (e.g. DABUR on 2026-01-22)
   is sold in full on its last day. We can't value it after that.
2. Cash flows: random deposits (invested at once) and withdrawals (raised by selling first).
3. Discretionary swap: at most once a month, with a small probability, one holding is
   replaced by an alternate from the mandate.
4. Quarterly rebalance: on the first trading day of Jan/Apr/Jul/Oct, back to target weights.
   Only positions that have drifted more than `drift_band` of NAV are traded, like a real
   manager avoiding needless turnover.

Constraints that always hold: whole shares only, trades only at the day's actual close and
only within a stock's listed dates, never short, and cash never below zero.
"""

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from portfolio.mandates import Mandate


@dataclass(frozen=True)
class SimConfig:
    fee_rate: float = 0.0003  # 0.03% of trade value
    cash_buffer: float = 0.01  # keep 1% of NAV in cash after a rebalance
    drift_band: float = 0.01  # rebalance a name only if it is >1% of NAV off target
    deposit_band: float = 0.0025  # tighter band when investing a deposit
    swap_prob: float = 0.15  # chance of a discretionary swap each month
    flows_per_year: float = 1.5  # average number of deposits/withdrawals per year
    flow_pct: tuple[float, float] = (0.02, 0.10)  # flow size as a share of NAV
    deposit_share: float = 0.65  # share of flows that are deposits


DEFAULT_CONFIG = SimConfig()


@dataclass
class Book:
    cash: float = 0.0
    holdings: dict[str, int] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    flows: list[dict] = field(default_factory=list)


def simulate(
    portfolio_id: int,
    mandate: Mandate,
    closes: pd.DataFrame,
    listing: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
    inception: pd.Timestamp,
    initial_cash: float,
    rng: np.random.Generator,
    cfg: SimConfig = DEFAULT_CONFIG,
) -> tuple[list[dict], list[dict]]:
    """Return (trades, cash_flows) as lists of dicts ready to insert."""
    calendar = closes.index[closes.index >= inception]
    marks = closes.ffill()
    dataset_end = closes.index[-1]
    exit_on = {s: last for s, (_, last) in listing.items() if last < dataset_end}
    active = dict(mandate.targets)
    book = Book()

    def price(s, d):
        return closes.at[d, s]

    def tradeable(s, d):
        first, last = listing[s]
        return first <= d <= last and not math.isnan(price(s, d))

    def can_target(s, d):
        return tradeable(s, d) and d < exit_on.get(s, dataset_end + pd.Timedelta(days=1))

    def nav(d):
        return book.cash + sum(q * marks.at[d, s] for s, q in book.holdings.items())

    def trade(d, s, side, q):
        px = float(price(s, d))
        value = q * px
        fee = round(value * cfg.fee_rate, 2)
        book.cash += -(value + fee) if side == "BUY" else value - fee
        book.holdings[s] = book.holdings.get(s, 0) + (q if side == "BUY" else -q)
        if book.holdings[s] == 0:
            del book.holdings[s]
        book.trades.append(
            dict(
                portfolio_id=portfolio_id,
                symbol=s,
                trade_date=d,
                side=side,
                quantity=q,
                price=px,
                fees=fee,
            )
        )

    def rebalance(d, band, reserve=0.0):
        total = nav(d)
        targets = {s: w for s, w in active.items() if can_target(s, d)}
        weight_sum = sum(targets.values())
        stuck = sum(q * marks.at[d, s] for s, q in book.holdings.items() if not tradeable(s, d))
        investable = max(0.0, (total - reserve) * (1 - cfg.cash_buffer) - stuck)
        want = {
            s: math.floor(w / weight_sum * investable / price(s, d)) for s, w in targets.items()
        }
        for s, have in sorted(book.holdings.items()):
            if not tradeable(s, d):
                continue
            target_q = want.get(s, 0)
            excess = have - target_q
            if excess > 0 and (target_q == 0 or excess * price(s, d) > band * total):
                trade(d, s, "SELL", excess)
        for s in sorted(want, key=lambda s: -targets[s]):
            have = book.holdings.get(s, 0)
            short_by = want[s] - have
            if short_by <= 0 or (have > 0 and short_by * price(s, d) <= band * total):
                continue
            # Buys never dip into the cash buffer (or the reserve for a pending withdrawal).
            # Without this, names left slightly overweight by the drift band keep their value
            # while new names get bought, and cash slowly runs to zero.
            keep = reserve + cfg.cash_buffer * (total - reserve)
            affordable = math.floor((book.cash - keep) / (price(s, d) * (1 + cfg.fee_rate)))
            q = min(short_by, affordable)
            if q > 0:
                trade(d, s, "BUY", q)

    flow_days = _schedule_flows(calendar, rng, cfg)
    month_starts = set(calendar.to_series().groupby(calendar.to_period("M")).min())
    quarter_starts = {d for d in month_starts if d.month in (1, 4, 7, 10)}

    for d in calendar:
        band = None
        if d == inception:
            book.cash += initial_cash
            book.flows.append(
                dict(portfolio_id=portfolio_id, date=d, amount=initial_cash, type="DEPOSIT")
            )
            band = 0.0

        for s in [s for s in book.holdings if exit_on.get(s) == d]:
            trade(d, s, "SELL", book.holdings[s])
            band = cfg.drift_band if band is None else band

        if d in flow_days:
            size = rng.uniform(*cfg.flow_pct) * nav(d)
            if rng.random() < cfg.deposit_share:
                amount = round(size, -3)
                book.cash += amount
                book.flows.append(
                    dict(portfolio_id=portfolio_id, date=d, amount=amount, type="DEPOSIT")
                )
                band = cfg.deposit_band if band is None else min(band, cfg.deposit_band)
            else:
                rebalance(d, 0.0, reserve=size)
                amount = math.floor(min(size, book.cash) / 1000) * 1000
                if amount > 0:
                    book.cash -= amount
                    book.flows.append(
                        dict(portfolio_id=portfolio_id, date=d, amount=-amount, type="WITHDRAWAL")
                    )

        if d in month_starts and d != inception and rng.random() < cfg.swap_prob:
            held_core = [s for s in active if s in book.holdings and tradeable(s, d)]
            fresh = [s for s in mandate.universe if s not in active and can_target(s, d)]
            if held_core and fresh:
                out, new = rng.choice(held_core), rng.choice(fresh)
                active[new] = active.pop(out)
                band = cfg.drift_band if band is None else band

        if d in quarter_starts:
            band = cfg.drift_band if band is None else band

        if band is not None:
            rebalance(d, band)

    return book.trades, book.flows


def _schedule_flows(calendar: pd.DatetimeIndex, rng: np.random.Generator, cfg: SimConfig):
    """Random deposit/withdrawal days: a Poisson number per calendar year, never on day one."""
    days = set()
    candidates = calendar[1:]
    for year in sorted(set(candidates.year)):
        in_year = candidates[candidates.year == year]
        n = min(rng.poisson(cfg.flows_per_year), len(in_year))
        days.update(rng.choice(in_year, size=n, replace=False))
    return {pd.Timestamp(d) for d in days}
