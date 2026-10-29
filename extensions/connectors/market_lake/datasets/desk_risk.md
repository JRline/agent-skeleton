---
name: desk_risk
description: Daily VaR, stressed VaR and expected shortfall of DemoBank's trading desks, in JPY millions.
required_filters: `metric` (or GROUP BY).
---
### Grain and keys
- One row per `as_of_date` x `desk_code` x `metric`; one value column, `value` (JPY millions).
- `desk_code` (e.g. DSK-161) is the market risk system's desk key. It is not a product_id: to compare with
  usage_limits, map it with the user's mapping table (rows with source_system = market: source_code = desk_code,
  product_id, location).
- `risk_class`: IR, FX or EQ.
### Filters needed for correct numbers
- `metric`: `VaR_1D_99` (1-day 99% VaR), `SVaR_1D_99` (stressed VaR) or `ES_10D_975` (10-day 97.5% expected
  shortfall). Different measures: never add them.
### Time
- `as_of_date` (YYYY-MM-DD): Tokyo business days from 2025-04-01 to 2026-03-27. Values are daily risk numbers:
  never add them across dates.
- VaR and ES are not additive across desks (diversification); a sum over desks is an undiversified total, and must
  be called that.
