# DAX/closehedgingflow_long

T1c long: first bar closing at or after 16:30 Berlin (1 h before the 17:30 close) on a weekday (<= 30 min), log(close / last close at or before the previous weekday's 17:30) > 0 -> LONG.
Card: docs/mldp/campaign/NASDAQ_2026-11/T1c_causal_entry_retest/PREREGISTRATION.md
Gate 0: D090 PASS under D092 firing rate, re-read 2026-11-10.
Scaffold: git d7b19b31^ XAUUSD/rangestructure_long (generic V2 runtime, feature schema verbatim).
