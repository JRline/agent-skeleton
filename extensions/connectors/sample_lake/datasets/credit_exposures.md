---
name: credit_exposures
description: Month-end credit risk RWA and EAD of DemoBank Group by credit portfolio, approach and internal rating grade, in JPY millions.
partition_column: partition_key
required_filters: `partition_key`, `entity` (or GROUP BY).
default_filters: partition_key = 'FY2025' AND entity = 'DemoBank Group'
---
### Grain and keys
- One row per `partition_key` x `as_of_date` x `entity` x `portfolio_code` x `approach` x `rating_grade`. Values:
  `ead` (exposure at default) and `rwa`, JPY millions; `currency` is JPY.
- `portfolio_code` (e.g. CP4037) is the credit system's portfolio key. It is not a product_id: to compare with
  usage_limits, map it with the user's mapping table (rows with source_system = credit: source_code =
  portfolio_code, product_id, location).
- `approach` is the usage_limits `component_code` of the credit risk components: `CRD_STD` (SA), `CRD_FND`
  (F-IRB), `CRD_ADV` (A-IRB), `CRD_SLT` (slotting). `rating_grade`: internal grade A (best) to E.
### Time
- `as_of_date`: one date per month: the last business day of each month with CLOSED RWA, i.e. 2025-04-30 ..
  2026-02-27, and 2026-03-26 for March 2026 (the latest CLOSED RWA date in usage_limits). Every one of these dates
  is also a usage_limits date. Balances: never add them across dates.
### Reconciliation
- Summed over rating grades, `rwa` equals usage_limits USED, CLOSED, BOOKED `amount` for the same date, mapped
  product and location, and component_code = approach. Counterparty credit risk, CVA and other RWA are not here.
