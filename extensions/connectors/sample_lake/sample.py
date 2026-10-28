"""Generate the synthetic dataset `usage_limits` (data/usage_limits.csv in this connector's folder): daily RWA and leverage exposure.

All data is synthetic and illustrative. It mimics a typical usage-and-limits dataset in a data lake:
- partitioned by fiscal year in `partition_key` (e.g. FY2025);
- one row per close-of-business date, scenario (ALLOC / USED), version (CLOSED / EARLY), method (BOOKED / DAILY /
  AVG60), measure leaf, region leaf and product leaf, with a single value column `amount` (JPY millions); rows
  exist only where the amount is non-zero; entity, rule and currency are columns (one group, JFSA B3, JPY);
- three hierarchies as one column per level: measure (resource > risk type > component, following
  the JFSA Pillar 3 templates OV1 for RWA and LR2 for the leverage exposure), region, and book (group >
  division > business line > product);
- Tokyo business days of fiscal year FY2025 (1 April 2025 to 31 March 2026);
- versions: EARLY usually arrives first and CLOSED later; some dates have CLOSED only; the newest dates have EARLY
  only. Each resource has its own latest dates (leverage lags RWA). Limits are CLOSED only;
- methods: FRTB internal-model market risk (MKT_INT) has DAILY (the day's value), AVG60 (60-business-day
  average) and BOOKED = max(DAILY, multiplier x AVG60), the binding amount; every other measure has BOOKED only.

    python scripts/setup_local.py      (or: python extensions/connectors/sample_lake/sample.py)
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "data" / "usage_limits.csv"
FY_START, FY_END = date(2025, 4, 1), date(2026, 3, 31)       # fiscal year: 1 April to 31 March
ENTITY, RULE, CURRENCY, PARTITION = "DemoBank Group", "JFSA B3", "JPY", "FY2025"
# latest dates loaded per resource and version (CLOSED lags EARLY; leverage lags RWA)
LAST = {("RWA", "CLOSED"): date(2026, 3, 26), ("RWA", "EARLY"): date(2026, 3, 31),
        ("Leverage exposure", "CLOSED"): date(2026, 3, 24), ("Leverage exposure", "EARLY"): date(2026, 3, 27)}
FLASH_SHARE, FLASH_RECENT = 0.35, 10      # EARLY kept for ~35% of older dates and every one of the last 10
IMA_MULTIPLIER, IMA_WINDOW = 1.5, 60
# Tokyo bank holidays in FY2025 (national holidays, substitute days, and the year-end closure 31 Dec - 3 Jan)
HOLIDAYS = {date(2025, 4, 29), date(2025, 5, 5), date(2025, 5, 6), date(2025, 7, 21), date(2025, 8, 11),
            date(2025, 9, 15), date(2025, 9, 23), date(2025, 10, 13), date(2025, 11, 3), date(2025, 11, 24),
            date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 12), date(2026, 2, 11),
            date(2026, 2, 23), date(2026, 3, 20)}

# component_code -> (resource, risk_type, component)
MEASURES = {
    "CRD_STD": ("RWA", "Credit risk (excl. CCR)", "Standardised approach (SA)"),
    "CRD_FND": ("RWA", "Credit risk (excl. CCR)", "Foundation IRB approach (F-IRB)"),
    "CRD_ADV": ("RWA", "Credit risk (excl. CCR)", "Advanced IRB approach (A-IRB)"),
    "CRD_SLT": ("RWA", "Credit risk (excl. CCR)", "Supervisory slotting approach"),
    "CPT_STD": ("RWA", "Counterparty credit risk (CCR)", "SA-CCR"),
    "CPT_OTH": ("RWA", "Counterparty credit risk (CCR)", "Other CCR (incl. SFTs)"),
    "CVA_STD": ("RWA", "CVA risk", "Standardised approach (SA-CVA)"),
    "CVA_BAS": ("RWA", "CVA risk", "Basic approach (BA-CVA)"),
    "FND_LOOK": ("RWA", "Equity investments in funds", "Look-through approach"),
    "FND_MAND": ("RWA", "Equity investments in funds", "Mandate-based approach"),
    "FND_FALL": ("RWA", "Equity investments in funds", "Fall-back approach"),
    "SET_ALL": ("RWA", "Settlement risk", "Settlement risk"),
    "SEC_INT": ("RWA", "Securitisation exposures (banking book)", "SEC-IRBA"),
    "SEC_EXT": ("RWA", "Securitisation exposures (banking book)", "SEC-ERBA (incl. IAA)"),
    "SEC_STD": ("RWA", "Securitisation exposures (banking book)", "SEC-SA"),
    "MKT_STD": ("RWA", "Market risk", "Standardised approach (FRTB-SA)"),
    "MKT_INT": ("RWA", "Market risk", "Internal models approach (FRTB-IMA)"),
    "OPR_STD": ("RWA", "Operational risk", "Standardised approach"),
    "THR_250": ("RWA", "Amounts below deduction thresholds (250% RW)", "Amounts below deduction thresholds (250% RW)"),
    "EXP_ONB": ("Leverage exposure", "On-balance sheet exposures", "On-balance sheet items (excl. derivatives and SFTs)"),
    "EXP_DRC": ("Leverage exposure", "Derivative exposures", "Replacement cost (RC)"),
    "EXP_DPF": ("Leverage exposure", "Derivative exposures", "Potential future exposure (PFE)"),
    "EXP_SGA": ("Leverage exposure", "Securities financing transaction exposures", "Gross SFT assets"),
    "EXP_SCR": ("Leverage exposure", "Securities financing transaction exposures", "CCR exposure for SFT assets"),
    "EXP_OBS": ("Leverage exposure", "Other off-balance sheet exposures", "Off-balance sheet items (after credit conversion factors)"),
}
REGIONS = {"Tokyo": "Japan", "Osaka": "Japan", "Hong Kong": "Asia Pacific", "Singapore": "Asia Pacific",
           "New York": "Americas", "London": "EMEA"}
REGION_SCALE = {"Tokyo": 1.0, "Osaka": 0.35, "Hong Kong": 0.45, "Singapore": 0.3, "New York": 0.55, "London": 0.5}
# product (product) -> division, business line, product_id, regions, Tokyo-sized amounts by measure
PRODUCTS = {
    "Term Loans": ("Corporate & Investment Banking", "Corporate Lending", "CIB-CL-TL",
                   ["Tokyo", "Osaka", "Hong Kong", "Singapore", "New York", "London"],
                   {"CRD_ADV": 21000, "CRD_STD": 800, "OPR_STD": 1700, "EXP_ONB": 52000, "EXP_OBS": 6000}),
    "Revolving Facilities": ("Corporate & Investment Banking", "Corporate Lending", "CIB-CL-RF",
                             ["Tokyo", "Hong Kong", "New York", "London"],
                             {"CRD_ADV": 7600, "CRD_STD": 300, "OPR_STD": 600, "EXP_ONB": 9000, "EXP_OBS": 15000}),
    "Project Finance": ("Corporate & Investment Banking", "Structured Finance", "CIB-SF-PF",
                        ["Tokyo", "Singapore", "London"],
                        {"CRD_SLT": 9400, "OPR_STD": 750, "EXP_ONB": 14000, "EXP_OBS": 2500}),
    "Securitisation Notes": ("Corporate & Investment Banking", "Structured Finance", "CIB-SF-SN",
                             ["Tokyo", "New York"],
                             {"SEC_INT": 1800, "SEC_EXT": 900, "SEC_STD": 400, "OPR_STD": 250,
                              "EXP_ONB": 11000}),
    "Residential Mortgages": ("Retail & Commercial Banking", "Mortgages", "RCB-MG-RM", ["Tokyo", "Osaka"],
                              {"CRD_ADV": 15800, "OPR_STD": 1900, "EXP_ONB": 61000, "EXP_OBS": 1500}),
    "SME Loans": ("Retail & Commercial Banking", "SME Banking", "RCB-SME-SL", ["Tokyo", "Osaka"],
                  {"CRD_STD": 9700, "CRD_FND": 2200, "OPR_STD": 1450, "EXP_ONB": 18000, "EXP_OBS": 3000}),
    "IR Swaps": ("Global Markets", "Rates", "GM-RT-IRS", ["Tokyo", "Hong Kong", "Singapore", "New York", "London"],
                 {"CPT_STD": 2600, "CVA_STD": 1100, "MKT_INT": 2500, "MKT_STD": 1400,
                  "CRD_ADV": 300, "OPR_STD": 790, "EXP_DRC": 3500, "EXP_DPF": 6200}),
    "Government Bonds": ("Global Markets", "Rates", "GM-RT-GB", ["Tokyo", "New York", "London"],
                         {"MKT_INT": 1500, "MKT_STD": 1200, "OPR_STD": 270, "EXP_ONB": 45000}),
    "FX Forwards": ("Global Markets", "FX", "GM-FX-FWD", ["Tokyo", "Hong Kong", "Singapore", "New York", "London"],
                    {"CPT_STD": 1800, "CVA_STD": 700, "MKT_STD": 1600, "SET_ALL": 60, "CRD_ADV": 200,
                     "OPR_STD": 430, "EXP_DRC": 2600, "EXP_DPF": 8100}),
    "FX Options": ("Global Markets", "FX", "GM-FX-OPT", ["Tokyo", "Hong Kong", "London"],
                   {"CPT_STD": 900, "CVA_STD": 400, "MKT_STD": 900, "MKT_INT": 1200, "OPR_STD": 340,
                    "EXP_DRC": 900, "EXP_DPF": 2400}),
    "Equity Derivatives": ("Global Markets", "Equities", "GM-EQ-DER", ["Tokyo", "Hong Kong", "London"],
                           {"CPT_STD": 1200, "CVA_STD": 500, "MKT_INT": 2200, "MKT_STD": 1100,
                            "OPR_STD": 500, "EXP_DRC": 1300, "EXP_DPF": 3900}),
    "Repo & Securities Lending": ("Global Markets", "Financing", "GM-FN-REPO", ["Tokyo", "New York", "London"],
                                  {"CPT_OTH": 1500, "CVA_STD": 200, "OPR_STD": 170,
                                   "EXP_SGA": 38000, "EXP_SCR": 2100}),
    "HQLA Bonds": ("Treasury & ALM", "Liquidity Portfolio", "TA-LP-HQ", ["Tokyo", "New York", "London"],
                   {"CRD_STD": 1400, "OPR_STD": 60, "EXP_ONB": 82000}),
    "Banking Book Hedges": ("Treasury & ALM", "ALM", "TA-ALM-BH", ["Tokyo", "London"],
                            {"CPT_STD": 600, "CVA_BAS": 300, "OPR_STD": 40,
                             "EXP_DRC": 700, "EXP_DPF": 1600}),
    "Fund Investments": ("Treasury & ALM", "Investment Portfolio", "TA-IP-FUND", ["Tokyo"],
                         {"FND_LOOK": 1300, "FND_MAND": 600, "FND_FALL": 250, "OPR_STD": 80,
                          "EXP_ONB": 2600}),
    "Strategic Investments": ("Treasury & ALM", "Investment Portfolio", "TA-IP-STR", ["Tokyo"],
                              {"THR_250": 900, "OPR_STD": 30, "EXP_ONB": 400}),
}
MARKET_LIKE = {"Global Markets"}
# Limit breaches to find: (product, region, measure, first date, last date, uplift on usage)
BREACHES = [("Equity Derivatives", "Hong Kong", "MKT_INT", date(2025, 11, 10), date(2025, 12, 19), 1.2),
            ("Repo & Securities Lending", "Tokyo", "EXP_SGA", date(2026, 2, 2), date(2026, 2, 20), 0.40),
            ("FX Options", "London", "MKT_STD", date(2026, 3, 2), date(2026, 3, 13), 0.60)]
# Limit-driven breaches: the quarter's limit is set below usage at the start of the quarter (usage itself is normal)
LIMIT_CUTS = [("Term Loans", "Tokyo", "CRD_ADV", "FY2025Q4", 0.96)]


def business_days() -> list[date]:
    days, d = [], FY_START
    while d <= FY_END:
        if d.weekday() < 5 and d not in HOLIDAYS:
            days.append(d)
        d += timedelta(days=1)
    return days


def fiscal_quarter(d: date) -> str:
    return f"FY2025Q{((d.month - 4) % 12) // 3 + 1}"


def generate() -> pd.DataFrame:
    rng = np.random.default_rng(20250401)
    days = business_days()
    n = len(days)
    t = np.arange(n) / (n - 1)
    quarters = np.array([fiscal_quarter(d) for d in days])
    flash_dates = {d for i, d in enumerate(days) if i >= n - FLASH_RECENT or rng.random() < FLASH_SHARE}
    rows = []
    for product, (division, line, code, regions, amounts) in PRODUCTS.items():
        for region in regions:
            scale = REGION_SCALE[region] * rng.uniform(0.85, 1.15)
            growth = rng.uniform(-0.04, 0.12)                      # drift over the fiscal year
            vol = 0.006 if division in MARKET_LIKE else 0.002       # trading books move more day to day
            walk = np.cumsum(rng.normal(0, vol, n))
            level = (1 + growth * t + walk - walk.mean() * 0.5).clip(0.6)
            for measure, base in amounts.items():
                ima = measure == "MKT_INT"
                if measure == "OPR_STD":                           # calculated quarterly, constant in a quarter
                    usage = np.array([np.round(base * scale * (1 + 0.02 * int(q[-1]))) for q in quarters])
                else:
                    usage = np.round(base / (IMA_MULTIPLIER if ima else 1) * scale * level * rng.uniform(0.97, 1.03, n))
                for b_product, b_region, b_measure, start, end, up in BREACHES:
                    if (b_product, b_region, b_measure) == (product, region, measure):
                        usage = np.round(usage * (1 + up * np.array([start <= d <= end for d in days])))
                methods = {"BOOKED": usage}
                if ima:                                              # internal models: binding = max(spot, m x avg)
                    avg = np.round(pd.Series(usage).rolling(IMA_WINDOW, min_periods=1).mean().to_numpy())
                    methods = {"DAILY": usage, "AVG60": avg, "BOOKED": np.maximum(usage, np.round(IMA_MULTIPLIER * avg))}
                limit = np.zeros(n)                                  # set at the start of each fiscal quarter
                for q in sorted(set(quarters)):
                    idx = quarters == q
                    headroom = rng.uniform(1.18, 1.32)
                    for c_product, c_region, c_measure, c_quarter, factor in LIMIT_CUTS:
                        if (c_product, c_region, c_measure, c_quarter) == (product, region, measure, q):
                            headroom = factor
                    limit[idx] = np.ceil(methods["BOOKED"][idx][0] * headroom / 10) * 10
                resource = MEASURES[measure][0]
                for i, d in enumerate(days):
                    keys = (d, measure, region, division, line, product, code)
                    rows.append(_row(*keys, "ALLOC", "CLOSED", "BOOKED", limit[i]))
                    for method, values in methods.items():
                        if d <= LAST[(resource, "CLOSED")]:
                            rows.append(_row(*keys, "USED", "CLOSED", method, values[i]))
                        if d in flash_dates and d <= LAST[(resource, "EARLY")]:      # preliminary: a little off
                            rows.append(_row(*keys, "USED", "EARLY", method,
                                             np.round(values[i] * (1 + rng.normal(0, 0.004)))))
    df = pd.DataFrame(rows)
    return df.sort_values(["as_of_date", "scenario", "version", "method", "component_code", "product_id",
                           "location"]).reset_index(drop=True)


def _row(d, measure, region, division, line, product, code, scenario, version, method, amount) -> dict:
    g1, g2, g3 = MEASURES[measure]
    return {"partition_key": PARTITION, "as_of_date": d.isoformat(), "fiscal_quarter": fiscal_quarter(d),
            "entity": ENTITY, "rule": RULE, "currency": CURRENCY, "scenario": scenario, "version": version,
            "method": method,
            "resource": g1, "risk_type": g2, "component": g3, "component_code": measure,
            "region_global": "Global", "region_area": REGIONS[region], "location": region,
            "book_total": "Group Total", "division": division, "business_line": line,
            "product": product, "product_id": code, "amount": float(amount)}


def write(path: Path = OUT) -> pd.DataFrame:
    """The data is generated, never committed: a fresh clone (or a test run) builds the same file from the seed."""
    df = generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, float_format="%.0f")
    return df


if __name__ == "__main__":
    df = write()
    print(f"{OUT}: {len(df):,} rows, {df.as_of_date.nunique()} business days, {df.as_of_date.min()} to {df.as_of_date.max()}")
