---
name: usage_limits
description: Daily RWA and leverage exposure of DemoBank Group, usage and limits, by measure, region and book hierarchy, JFSA Basel III, in JPY millions.
partition_column: partition_key
required_filters: `partition_key`, `scenario` (or GROUP BY), `resource` (or GROUP BY), `version` (or GROUP BY), `method` (or GROUP BY), `entity` (or GROUP BY), `rule` (or GROUP BY), `currency` (or GROUP BY).
default_filters: partition_key = 'FY2025' AND entity = 'DemoBank Group' AND rule = 'JFSA B3' AND currency = 'JPY' AND version = 'CLOSED' AND method = 'BOOKED'
---
### Grain and keys
- Partitioned by fiscal year: `partition_key` = `FY2025` (the partition column). Always filter on `partition_key`: it is how the server reads only that year. describe_dataset lists the partitions that exist.
- One row per `partition_key` x `as_of_date` x `entity` x `rule` x `currency` x `scenario` x `version` x `method` x `component_code` (measure leaf) x `location` (region leaf) x `product_id` (book leaf).
- One value column, `amount`. Leaf rows only (no subtotal rows): any level of a hierarchy is a SUM over its leaves. Rows exist only where the amount is non-zero.
### Filters needed for correct numbers (each one double counts if left out)
- `entity` = `DemoBank Group`, `rule` = `JFSA B3`, `currency` = `JPY`: the only values today; filter on them anyway so queries stay correct when other entities, rules or currencies arrive.
- `resource`: the resource, `RWA` or `Leverage exposure`. Never add the two.
- `scenario`: `USED` (actual) or `ALLOC` (allocated limit). Never add the two; compare them side by side.
- `version`: `CLOSED` (recommended) or `EARLY` (preliminary). EARLY usually arrives first and CLOSED later; some dates have CLOSED only, and the newest dates have EARLY only. Use CLOSED unless the user asks for flash or for dates that have no CLOSED yet, and say which version a figure is. Never add the two.
- `method`: `BOOKED` for every figure unless the user asks otherwise. Only FRTB internal-model market risk (`MKT_INT`) also has `DAILY` (the day's value) and `AVG60` (60-business-day average); its BOOKED is the binding amount, max(DAILY, multiplier x AVG60), with a multiplier of 1.5 in this sample. Never add methods.
### Time and the latest date
- `as_of_date` (YYYY-MM-DD): close-of-business date, Tokyo business days only (no weekends, Japanese holidays, 31 Dec - 3 Jan). `fiscal_quarter`: `FY2025Q1` .. `FY2025Q4`.
- There is no "latest" flag. Each resource and version has its own latest date (CLOSED lags EARLY; leverage lags RWA). Find it with one query on the partition: SELECT resource, version, MAX(as_of_date) AS latest_date ... WHERE partition_key = 'FY2025' AND scenario = 'USED' AND method = 'BOOKED' AND entity = 'DemoBank Group' AND rule = 'JFSA B3' AND currency = 'JPY' GROUP BY resource, version.
- Month-end and quarter-end figures: the last business day on or before the calendar date (some calendar month-ends have no data).
- Daily amounts are balances: never add them across dates (use one date, an average, or the change between two dates).
### Hierarchies (first level = top)
- Measure: `resource` (RWA or leverage exposure) > `risk_type` risk type or exposure type > `component` approach or component; `component_code` is the leaf key (e.g. `MKT_INT`).
  - RWA (JFSA Pillar 3 OV1): Credit risk (excl. CCR) [SA, F-IRB, A-IRB, supervisory slotting]; Counterparty credit risk (CCR) [SA-CCR, other CCR incl. SFTs]; CVA risk [SA-CVA, BA-CVA]; Equity investments in funds [look-through, mandate-based, fall-back]; Settlement risk; Securitisation exposures (banking book) [SEC-IRBA, SEC-ERBA, SEC-SA]; Market risk [FRTB-SA, FRTB-IMA]; Operational risk [standardised approach]; Amounts below deduction thresholds (250% RW).
  - Leverage exposure (JFSA LR2): On-balance sheet exposures; Derivative exposures [replacement cost, potential future exposure]; Securities financing transaction exposures [gross SFT assets, CCR exposure]; Other off-balance sheet exposures [after credit conversion factors].
  - A level with no further breakdown repeats its parent's name (e.g. Settlement risk).
- Region: `region_global` Global > `region_area` Japan, Asia Pacific, Americas, EMEA > `location` Tokyo, Osaka, Hong Kong, Singapore, New York, London.
- Book: `book_total` Group Total > `division` division (Corporate & Investment Banking, Retail & Commercial Banking, Global Markets, Treasury & ALM) > `business_line` business line (e.g. Corporate Lending, Rates, Financing) > `product` product (e.g. Term Loans, IR Swaps); `product_id` is the product key (e.g. GM-FX-FWD).
- Not every product is booked in every region.
### Measures and notes
- `amount` in JPY millions.
- Operational risk RWA is calculated quarterly and allocated to products: constant within a fiscal quarter.
- Limits (scenario ALLOC) are CLOSED and BOOKED only, set per measure leaf, region and product at the start of each fiscal quarter. Utilisation = USED / ALLOC on the same keys.
- Entity-level items (capital deductions, the output floor adjustment) are not allocated to books and are not in this dataset.
