"""Display strings for numbers, so the explainer can quote rather than convert."""

import math


def inr(value: float) -> str:
    """Indian convention: crore (1e7) and lakh (1e5) for large amounts."""
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e7:
        return f"{sign}₹{v / 1e7:,.2f} Cr"
    if v >= 1e5:
        return f"{sign}₹{v / 1e5:,.2f} lakh"
    return f"{sign}₹{v:,.0f}"


def display(value, unit: str) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if unit == "%":
        return f"{value * 100:.2f}%"
    if unit == "INR":
        return inr(value)
    if unit == "ratio":
        return f"{value:.2f}"
    if unit == "count":
        return f"{value:.0f}"
    return str(value)
