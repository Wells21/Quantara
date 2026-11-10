# History extension: bars-only older years (design, 2026-11-05 -- for owner review before any code change)

Owner 2026-11-05 approved extending history ("approve all 5"; US500 / JPN "start them at 2015"; staging first; tick
files are not deleted until the owner decides). Downloads: shared/data/download_history_ticks.py into
assets/<ASSET>/raw_history_staging/ticks (queue: logs/history_download_queue_20261105.sh).

## Periods

| asset | old period (bars only after the decision) | tick-covered period (kept) |
|---|---|---|
| XAUUSD | 2005-01 .. 2021-12 | 2022-01 .. now |
| GBPJPY, USDMXN | 2012/13 .. 2021-12 | 2022-01 .. now |
| NASDAQ, DAX, USOIL | 2013 .. 2021-12 | 2022-01 .. now |
| HK | 2014 .. 2021-12 | 2022-01 .. now |
| US500, JPN | 2015 .. 2021-12 | 2022-01 .. now |
| BTCUSD | 2017-05 .. 2021-12 | 2022-01 .. now |

## Rebuild order per asset (after the staging check)

1. Staging check: size versus estimate, weekly coverage, empty weeks, the format equal to raw/ticks, and the overlap
   seam (no ticks at or after 2022-01-01).
2. Move the staged files into raw/ticks (same naming).
3. shared/data/tick_curation.py curate (causal quote filter; USOIL backward roll adjustment over the full span).
4. Forge the bars over the full span (streaming, causal, 50 bars/day target). Bars after early 2022 may differ
   slightly at the seam because of the 20-day tick EWMA. No edge is deployed, so nothing live depends on the old bars.
5. FFD d re-estimated on the full history (runbook Step 0C), then the HMM and the entropy encodings. Bar quality
   report.
6. Optional, only on owner approval: delete the old raw and curated tick files once 3-5 are verified.

## What uses ticks, and what happens in the old years

| component | tick use | old years |
|---|---|---|
| Gate 0 (gate0_tool Labeler) | none (bar OHLC, both-touch = SL) | full history |
| Gate A (v3_horizon_lab hybrid grid) | ties only, via model_rows tick resolver | no tick file -> fallback sl_first (owner 2026-11-05: "make it SL") |
| export / model_rows | ties only | same fallback |
| Gate C exact replay (5,700 ms, broker spread) | full replay | **tick-covered period only** |
| Task 2A label (mid replay) | tick replay | **NEW: bar-OHLC label when no ticks** (sl_first ties), flagged label_source=bars |
| Task 2A economics (executed_r) | tick replay | **tick-covered period only**; old rows executed_r = NaN, flagged |
| Steps 01-03 (training on the label, weights on label_t1) | via Task 2A | full history |
| Gate D v4.1 (OOF, weighted) | via Step 03 | full history |
| Steps 04-05 (calibrator, threshold economics) | executed_r | calibrator: full history (labels); economics: tick-covered rows only |
| PBO / DSR (Gate C trials) | Gate C | tick-covered period only |
| allocator 00d, cost model, live | ticks | unchanged (recent) |
| holdout / Gate E | own ticks | unchanged |

## Code changes needed (shown to the owner before they are made)

- Task 2A (shared/execution/executable_target_dataset.py):
  - label rows outside the tick catalogue from bar OHLC;
  - executed_r = NaN there;
  - the 99 % coverage rule applies to the tick-covered period only.
- Steps 04 / 05: economics on rows with executed_r only.
- The Gate C runner and the PBO driver: restrict to the tick-covered period (explicit start date), so execution
  realism is unchanged.
- Gate 0 tool: none (it already uses bar OHLC).
- Runbook Rev 47 + register: the history extension, plus the 4 approved gate changes (Gate 0 / drift re-check 90 %,
  Gate D fail -> primary-only, FWER report-only, strategy risk 0.10).

## Statistical note

Gate 0 power rises with sqrt(years): NASDAQ 13.3 / 4.2 years -> SE x 0.56; gold 21 / 4.3 -> SE x 0.45. The smallest
detectable effect falls from about 0.07 R to about 0.04 R (NASDAQ) and 0.03 R (gold).
