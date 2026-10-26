---
name: limit_status
description: Where RWA or leverage usage stands against its limits now, from the usage_limits view: usage, limit, headroom and utilisation per key at the latest date, and any key over its limit.
toolkits: datalake, analysis
source: data lake view usage_limits
references: references/datalake_dictionary.md
when_to_use: The user asks whether anything is over or close to its limit now, or for the latest usage against limits or headroom at some level. Not for breaches over a period or what caused a breach (use limit_breach_analysis), and not for an attached file.
---
## Steps
1. Settle the resource (RWA, leverage or both). The default key is the grain limits are set at: product_id x location (the region leaf, e.g. Tokyo) x component_code. Add another level (division, Group total, a product across regions) only when the user names it. The date is the latest CLOSED date of each resource; it needs no confirmation.
2. One sql_query at that date: SUM usage and limit to the keys (one SELECT per level when the user named extra levels, combined with UNION ALL and a level column), keeping component_code as a key, and return usage, limit, headroom (limit - usage) and utilisation (usage / limit) for every key. save_as limit_status.
3. Final answer from limit_status, listing it in "tables".

## Checks
- Usage and limit are compared on the same keys, date, version CLOSED and method BOOKED, per component_code.
- Each level is judged on its own sums: a product can breach while its division and the Group total do not, and the reverse.
- Excess (usage - limit) is named only for a key over its limit; headroom and excess are never added up across keys or levels.

## Output
A headline with the number of keys over their limit and the date of each resource. Each key over its limit with its excess (JPY millions), then the keys closest to their limit (highest utilisation, with headroom). Table: "Limit status (CLOSED, JPY m)".
