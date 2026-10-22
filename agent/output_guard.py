"""Guardrail layer 3b: every number in the explanation must come from the computed results.

The explainer is told to quote, not calculate. This module checks that it did. Each number
in the answer must match some number in the results after one of the conversions an
explanation legitimately uses: fraction to percent (x100), rupees to thousand/lakh/crore,
dropping the sign ("a loss of 4.9 lakh"), within the rounding the answer shows. Dates,
years, small counts and numbers the manager wrote themselves are allowed.

It can't prove the prose is right, but it catches the failure that matters most in a
financial answer: a plausible number that was never computed.
"""

import math
import re
from collections.abc import Iterable

NUMBER = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?")
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
DATE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{4}}-\d{{2}}\b"  # 2025-12-31, 2025-12
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}"  # 31 December, 1st Jan
    rf"|\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b",  # December 31
    re.I,
)
SCALES = (1.0, 100.0, 1e-3, 1e-5, 1e-7, 1e-6)
SMALL_INT_LIMIT = 12  # "top 5", "3 portfolios", "12 months"

DISCLAIMER = (
    "\n\n---\n*Figures are computed from synthetic portfolios on real NSE prices "
    "(price returns; dividends not included). This is not investment advice.*"
)


def numbers_in_text(text: str) -> list[tuple[str, float, int]]:
    """(as written, value, decimals shown) for each number, skipping dates."""
    text = DATE.sub(" ", text)
    out = []
    for m in NUMBER.finditer(text):
        raw = m.group(0)
        clean = raw.replace(",", "").replace("−", "-")
        try:
            value = float(clean)
        except ValueError:
            continue
        decimals = len(clean.split(".")[1]) if "." in clean else 0
        out.append((raw, value, decimals))
    return out


def collect_numbers(obj) -> set[float]:
    """Every number anywhere in a nested result payload, including inside strings."""
    found: set[float] = set()

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, int | float):
            if not (isinstance(x, float) and math.isnan(x)):
                found.add(float(x))
        elif isinstance(x, str):
            for _, v, _ in numbers_in_text(x):
                found.add(v)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list | tuple):
            found.add(float(len(x)))
            for v in x:
                walk(v)

    walk(obj)
    return found


def _supported(value: float, decimals: int, sources: Iterable[float]) -> bool:
    tolerance = 0.5 * 10 ** (-decimals) + 1e-9
    for s in sources:
        for k in SCALES:
            if abs(abs(value) - abs(s * k)) <= tolerance:
                return True
    return False


def unsupported_numbers(answer: str, payload, question: str = "") -> list[str]:
    sources = collect_numbers(payload) | {v for _, v, _ in numbers_in_text(question)}
    bad = []
    for raw, value, decimals in numbers_in_text(answer):
        if decimals == 0 and abs(value) <= SMALL_INT_LIMIT:
            continue
        if decimals == 0 and 1990 <= value <= 2100:
            continue  # a year
        if not _supported(value, decimals, sources):
            bad.append(raw)
    return bad
