---
name: usage_comparison
description: Compare a financial resource (RWA or leverage exposure) between two dates for any slice - division, product, region, risk type - broken down by any hierarchy level, with the drivers of the change. Also for zooming in on an earlier comparison.
toolkits: datalake, analysis
source: data lake view usage_limits
references: references/datalake_dictionary.md
when_to_use: The user asks how RWA or leverage exposure differs between two dates or periods (day on day, month end, quarter end, year to date), what drove a change for a book, region or risk type, or to drill into a change found earlier in the conversation.
---
## Steps
1. Settle the parameters from the request and the conversation: the resource (RWA unless leverage is asked), scenario USED, version CLOSED and method BOOKED (unless asked otherwise), the slice, the breakdown level and the two dates. A month or quarter end is the last business day on or before it; "latest" is that resource's latest CLOSED date, and "previous day" the business day before it. Reuse dates and figures already found earlier in the conversation instead of querying them again. If the dates or the slice cannot be inferred, ask the user.
2. One sql_query with every required filter, the slice, only the two dates (as_of_date IN (...)) and GROUP BY as_of_date plus every level the analysis will need: when the user may drill down (e.g. by risk type and approach), include the deeper level now so no second query is needed. If a date is "latest" and not yet known, find it in the same query (a subquery on MAX(as_of_date)) rather than in a separate one. save_as resource_two_dates.
3. run_python on resource_two_dates: pivot to one row per breakdown value with one column per date (column names are the dates), then change and change_pct (round 2); roll up to the coarser level too if the query was at a deeper one; add a Total row; sort by absolute change. save_as resource_comparison.
4. Final answer from the table, listing resource_comparison in "tables" with a title such as "RWA usage by risk type, 2026-03-25 vs 2026-03-26 (CLOSED, JPY m)".

## Checks
- Both dates exist for that resource and version, and every required filter was applied once.
- The breakdown rows add up to the Total row.
- Every number in the answer cites the run_python or sql_query ref; percentages come from the table.
- The headline direction (rose / fell) matches the sign of the total change.

## Output
A one-line headline: resource, total change in JPY millions and %, the two dates and the version. Then 2-4 bullets on the largest drivers by amount, each with its change and %. Mention something unusual only if it is in this data (a row that appears or disappears, a sign change, one item making up most of the change); do not explain general properties of percentages or shares. Do not repeat the table in the text.
