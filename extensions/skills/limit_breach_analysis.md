---
name: limit_breach_analysis
description: Find limit breaches in the usage_limits dataset (RWA or leverage usage above its limit) at any level, and identify the source of each - a usage increase (which component), a limit reduction at a quarterly reset, or for internal-model market risk the 60-day average keeping the figure high after a spike.
toolkits: datalake, analysis
datasets: usage_limits
when_to_use: The user asks which books, products or regions breached (or exceeded) their RWA or leverage limits over a period, when, or what caused a breach. Not for whether anything is over its limit now (use limit_status), not for limit headroom without a breach question, and not for an attached file.
---
## Steps
1. Settle the parameters: the resource (RWA, leverage or both), the level to check (product and region unless the user names another, e.g. division), the period (confirm with the user if open), version CLOSED and method BOOKED.
2. One sql_query that finds the breaches in SQL, never pulling daily detail: join USED and ALLOC on the same keys and date at the chosen level (SUM both to that level first, keeping component_code as a key: limits are set per component, and a sum over components can hide a breach), keep rows where usage > limit, and return per breaching key: first and last breach date, number of breach days, the largest excess (usage - limit) and its date. save_as breaches.
3. One sql_query for the breaching keys only, at component level: usage and limit on the business day before each first breach date (the latest as_of_date in the data before it) and on each first breach date (as_of_date IN those dates), so the change at the start of each breach is visible. For an MKT_INT breach, also its DAILY and AVG60 (method IN ('DAILY', 'AVG60', 'BOOKED')) on those dates and on the last breach date. save_as breach_start.
4. run_python on breach_start: for each breach, the change in usage and in limit from the day before to the first breach day, and which component (component) moved most; classify the source as "usage increase" (name the component), "limit reduction" (the limit fell, usually at a quarter start, with usage about flat) or "both"; for internal-model market risk, say whether DAILY or the multiplier x AVG60 was binding at the start and at the end of the breach. save_as breach_sources.
5. Final answer from breaches and breach_sources, listing both in "tables".

## Checks
- Usage and limit are compared on the same keys, date, version CLOSED and method BOOKED, at the same level.
- Breach dates and counts come from the sql_query refs; every source classification is backed by the usage and limit change in breach_sources.
- A limit-driven breach is not described as a usage increase, and the reverse.

## Output
A headline with the number of breaches found and the period checked. Then one bullet per breach: who (product / region / component), from when to when (business days), the largest excess (JPY millions, with date), and its source in one plain sentence with the figures (e.g. "limit cut from 29,810 to 22,110 at the FY2025Q4 reset while usage stayed about 23,000"). Tables: "Breaches found (CLOSED, JPY m)" and "Source of each breach".
