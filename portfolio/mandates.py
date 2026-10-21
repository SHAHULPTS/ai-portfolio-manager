"""The synthetic book: who manages what, and what each portfolio is allowed to hold.

Each mandate has target weights (the core holdings) and alternates (names the manager may
rotate into on a discretionary swap). Weights are renormalised over whatever is listed and
trading on a given day, so a target stock that lists later (e.g. SBILIFE in 2017) joins at
the first rebalance after its listing.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mandate:
    name: str
    benchmark: str
    targets: dict[str, float]
    alternates: tuple[str, ...]

    @property
    def universe(self) -> tuple[str, ...]:
        return tuple(self.targets) + self.alternates


MANDATES = {
    "banking": Mandate(
        name="Banking & Financials Large Cap",
        benchmark="NIFTYBANK",
        targets={
            "HDFCBANK": 0.18,
            "ICICIBANK": 0.16,
            "KOTAKBANK": 0.10,
            "AXISBANK": 0.10,
            "SBIN": 0.10,
            "BAJFINANCE": 0.08,
            "INDUSINDBK": 0.06,
            "BANKBARODA": 0.05,
            "HDFCLIFE": 0.05,
            "SBILIFE": 0.04,
            "CHOLAFIN": 0.04,
            "BAJAJFINSV": 0.04,
        },
        alternates=("PNB", "CANBK", "MUTHOOTFIN", "SHRIRAMFIN", "ICICIGI", "HDFCAMC", "UNIONBANK"),
    ),
    "balanced": Mandate(
        name="Balanced Nifty Core",
        benchmark="NIFTY50",
        targets={
            "RELIANCE": 0.12,
            "HDFCBANK": 0.12,
            "ICICIBANK": 0.09,
            "INFY": 0.08,
            "TCS": 0.07,
            "ITC": 0.06,
            "LT": 0.06,
            "BHARTIARTL": 0.06,
            "SBIN": 0.05,
            "AXISBANK": 0.05,
            "KOTAKBANK": 0.05,
            "HINDUNILVR": 0.05,
            "MARUTI": 0.04,
            "SUNPHARMA": 0.04,
            "ASIANPAINT": 0.03,
            "TITAN": 0.03,
        },
        alternates=(
            "ULTRACEMCO",
            "BAJFINANCE",
            "MM",
            "NTPC",
            "POWERGRID",
            "TATASTEEL",
            "ADANIPORTS",
        ),
    ),
    "tech": Mandate(
        name="IT & Technology Growth",
        benchmark="NIFTY50",
        targets={
            "TCS": 0.20,
            "INFY": 0.20,
            "HCLTECH": 0.14,
            "WIPRO": 0.10,
            "TECHM": 0.10,
            "LTM": 0.10,
            "NAUKRI": 0.08,
            "BHARTIARTL": 0.08,
        },
        alternates=("ETERNAL", "DMART", "TRENT", "INDIGO"),
    ),
    "defensive": Mandate(
        name="Defensive FMCG & Pharma",
        benchmark="NIFTY50",
        targets={
            "HINDUNILVR": 0.14,
            "ITC": 0.14,
            "NESTLEIND": 0.10,
            "BRITANNIA": 0.08,
            "DABUR": 0.06,
            "GODREJCP": 0.06,
            "SUNPHARMA": 0.10,
            "DRREDDY": 0.08,
            "CIPLA": 0.08,
            "DIVISLAB": 0.08,
            "APOLLOHOSP": 0.08,
        },
        alternates=("TATACONSUM", "TORNTPHARM", "ZYDUSLIFE", "MAXHEALTH", "VBL", "UNITDSPR"),
    ),
    "consumption": Mandate(
        name="Consumption & Auto",
        benchmark="NIFTY50",
        targets={
            "MARUTI": 0.12,
            "MM": 0.12,
            "TMPV": 0.08,
            "BAJAJ-AUTO": 0.08,
            "EICHERMOT": 0.08,
            "HEROMOTOCO": 0.06,
            "TVSMOTOR": 0.06,
            "TITAN": 0.10,
            "ASIANPAINT": 0.08,
            "TRENT": 0.08,
            "DMART": 0.08,
            "HAVELLS": 0.06,
        },
        alternates=("INDHOTEL", "ETERNAL", "BOSCHLTD", "MOTHERSON", "VBL", "PIDILITIND"),
    ),
    "psu_energy": Mandate(
        name="PSU & Energy",
        benchmark="NIFTY50",
        targets={
            "NTPC": 0.12,
            "POWERGRID": 0.12,
            "ONGC": 0.10,
            "COALINDIA": 0.10,
            "IOC": 0.08,
            "BPCL": 0.08,
            "GAIL": 0.08,
            "SBIN": 0.08,
            "BEL": 0.08,
            "PFC": 0.08,
            "RECLTD": 0.08,
        },
        alternates=("NHPC", "BHEL", "HAL", "IRFC", "IRCTC", "BANKBARODA", "CANBK", "LICI"),
    ),
    "infra": Mandate(
        name="Infrastructure & Capital Goods",
        benchmark="NIFTY50",
        targets={
            "LT": 0.16,
            "ULTRACEMCO": 0.10,
            "GRASIM": 0.08,
            "ADANIPORTS": 0.08,
            "SIEMENS": 0.08,
            "ABB": 0.08,
            "CUMMINSIND": 0.07,
            "BEL": 0.07,
            "HAL": 0.07,
            "DLF": 0.07,
            "TATASTEEL": 0.07,
            "JSWSTEEL": 0.07,
        },
        alternates=("AMBUJACEM", "SHREECEM", "CGPOWER", "BHEL", "LODHA", "HINDALCO", "JINDALSTEL"),
    ),
}

# Fictional managers.
MANAGERS = {1: "Asha Menon", 2: "Ravi Iyer", 3: "Priya Shah"}

# (portfolio_id, manager_id, portfolio name, mandate key, initial cash in INR)
BOOK = [
    (101, 1, "Asha Financials Fund", "banking", 20_000_000),
    (102, 1, "Asha Core Equity", "balanced", 50_000_000),
    (201, 2, "Ravi Tech Opportunities", "tech", 10_000_000),
    (202, 2, "Ravi Defensive Income", "defensive", 20_000_000),
    (203, 2, "Ravi Consumer Trends", "consumption", 10_000_000),
    (301, 3, "Priya PSU & Energy", "psu_energy", 10_000_000),
    (302, 3, "Priya Infrastructure", "infra", 20_000_000),
]
