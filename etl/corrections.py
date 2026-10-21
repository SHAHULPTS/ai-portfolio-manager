"""Hand-checked fixes for known errors in the source data.

Each entry was found by `python -m etl.verify_load` (missing days and one-day moves over 25%)
and confirmed by looking at the surrounding prices. DESIGN_NOTES.md, phase 1, has the evidence.
Add to these lists rather than patching the database by hand, so a rebuild stays correct.
"""

# The source has junk prices for these stocks from 2015 until they actually listed.
# Bars before the real NSE listing date are dropped.
LISTING_DATES = {
    "SBILIFE": "2017-10-03",
    "HDFCLIFE": "2017-11-17",
    "IRCTC": "2019-10-14",
    "IRFC": "2021-01-29",
}

# Single days with an unadjusted price (2x-4x the days either side). The day is dropped.
BAD_DAYS = [
    ("INFY", "2015-04-24"),
    ("MOTHERSON", "2015-07-22"),
    ("BEL", "2015-09-11"),
    ("DIVISLAB", "2015-09-22"),
    ("ZYDUSLIFE", "2015-10-05"),
]

# Demergers the source did not adjust for. All prices before the ex-date are scaled so that
# the stock's ex-date return equals the NIFTY 50's return that day:
#     factor = (close_ex / close_prev) / (nifty_ex / nifty_prev)
# i.e. we assume the stock moved with the market that day and attribute the rest of the drop to
# the demerger. (SIEMENS's ex-date was the 2025-04-07 tariff crash, NIFTY -3.2%; a plain price
# ratio would have erased that real fall.)
# ITC Hotels (2025-01-06) and Jio Financial (2023-07-20) are absent on purpose: NSE ran special
# price-discovery sessions for them, so the source prices already reflect the split.
DEMERGERS = [
    ("ADANIENT", "2015-06-03"),  # Adani Ports and Adani Power split off
    ("CGPOWER", "2016-03-15"),  # Crompton Greaves Consumer Electricals split off
    ("GRASIM", "2017-07-19"),  # Aditya Birla Capital split off
    ("SIEMENS", "2025-04-07"),  # Siemens Energy India (ENRIN) split off
    ("TMPV", "2025-10-14"),  # Tata Motors commercial vehicles (TMCV) split off
]
