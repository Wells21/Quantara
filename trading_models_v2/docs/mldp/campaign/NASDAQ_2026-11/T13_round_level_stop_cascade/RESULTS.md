# T13 results: round-level stop-cascade continuation (Gate 0 test 26, run 2026-11-07)

Card: PREREGISTRATION.md (written before any run). Cell H1_V1. Outputs: gate0_T13.json (full history),
gate0_T13_2014_2021.json, gate0_T13_2022_on.json. Rule: Rev 47 (pooled drift + pooled C1 at one-sided 90 %, z 1.28;
C2 diff > 0; the asset's own diffs > 0) + recency (the asset's own drift diff > 0 on 2022+).

## Pooled (10 assets)

| side | n | drift diff (se) | lb at 1.28 | C1 diff (se) | lb at 1.28 | C2 diff | pooled |
|---|---|---|---|---|---|---|---|
| LONG | 8,023 | +0.020 (0.012) | +0.004 | +0.024 (0.011) | +0.009 | +0.012 | **PASS** |
| SHORT | 7,808 | -0.010 (0.013) | -0.026 | +0.022 (0.013) | +0.006 | +0.004 | FAIL (drift) |

The pooled LONG pass does not come from one period. Pooled drift is +0.013 in 2014-2021 and +0.020 in 2022+.

## Verdict by asset (Rev 47 + recency)

| asset | LONG | own drift full / 2022+ | own C1 full / 2022+ | SHORT |
|---|---|---|---|---|
| **XAUUSD** | **PASS** | +0.073 (0.025) / +0.064 (0.044) | +0.074 (0.023) / +0.019 (0.034) | FAIL |
| **HK** | **PASS** | +0.077 (0.057) / +0.072 (0.086) | +0.131 (0.059) / +0.137 (0.086) | FAIL |
| **JPN** | **PASS** | +0.042 (0.053) / +0.030 (0.066) | +0.051 (0.052) / +0.054 (0.065) | FAIL |
| **BTCUSD** | **PASS** | +0.059 (0.041) / +0.029 (0.052) | +0.011 (0.036) / -0.043 (0.042) | FAIL |
| NASDAQ | FAIL | -0.018 (0.036) / +0.014 (0.046) | -0.051 (0.031) / +0.020 (0.038) | FAIL |
| US500, DAX, GBPJPY, USDMXN, USOIL | FAIL | | | FAIL |

All four LONG passes clear the recency condition. NASDAQ fails on its own diffs over the full history. They are
negative in 2014-2021 and positive (not significant) in 2022+.

## Reading (honest)

- **XAUUSD carries the pooled pass.** It is the only asset whose own diffs are individually significant: drift z 2.9,
  C1 z 3.2, in both periods. Gold is the market where Osler's round-number stop clustering is best documented (dealer
  FX books), so this fits the mechanism.
- **The roundness-specific part (C1) in gold is weaker in 2022+**: +0.019, se 0.034. The breakout continuation
  holds up recently, but the extra lift from the level being ROUND is thin in the period Gate C prices.
- **HK and JPN are positive but noisy** (z 0.6-2.2, about 400 events each).
- **BTCUSD passes only on drift.** Its C1 is negative in 2022+ (-0.043): a 24 h breakout does better through the
  shifted, non-round grid. That is a warning sign for the round-level mechanism on BTCUSD.
- **Shorts fail everywhere pooled.** Down-breaks through round levels do not continue. The asymmetry has a plausible
  story (buy-stop cascades above long-held highs are short covering), but it was not pre-registered, so it is
  recorded as an observation only.
- **Trial count:** 26 Gate 0 tests in this campaign. A pooled z of 1.6 is weak evidence on its own; the later DSR,
  PBO and FWER (report-only) charges at Gates A-C have to account for every one of the 26.

## Status

Gate 0 PASS for T13 LONG on XAUUSD, HK, JPN and BTCUSD. FAIL on NASDAQ (the campaign's target) and on all shorts.
The next step (Gate A on the extended history) needs the HISTORY_EXTENSION_DESIGN.md code changes and an owner
scope decision (the campaign rule is NASDAQ-first).

## Gate A / Gate C (2026-11-09)

Edges: <ASSET>/roundlevelcascade_long. The trigger reproduces the Gate 0 events exactly (XAUUSD 1913, HK 334, JPN 427).

| | XAUUSD | HK | JPN |
|---|---|---|---|
| Gate A (405 cells, full history, cost = broker mean) | 405 / 405 net > 0 (median +0.098 R) | 402 / 405 (median +0.050) | 403 / 405 (median +0.079) |
| Gate C 2022+ trades per cell | ~670 (running) | ~140 | ~263 |
| Gate C 2022+ winner (exec mean R, 95 % CI) | running | H1_E150_V48_PT1.5_SL1 +0.145 (-0.05, +0.32) | H4_E100_V2_PT1_SL1 +0.076 (-0.02, +0.16) |
| PBO (405 trials) / Prob[OOS <= 0] | running | 0.43 / 0.53 | 0.74 / 0.56 |
| Verdict | -> Gate C on the full history with era-scaled cost | **KILLED_PBO** | **KILLED_PBO** |

Owner 2026-11-09: HK and JPN are killed at PBO, with no full-history re-run ("they already failed the PBO"). Their
partial full-history runs were stopped, and the folders are archived to
Desktop\Quantara_edge_archive_2026-11-09\<ASSET>\roundlevelcascade_long. XAUUSD continues under the new standing rule
(full history decides, with era-scaled broker cost; the 2022+ locked-cell exec mean must be > 0).

### XAUUSD Gate C 2022+ (broker cost model, 5,700 ms, 405 cells; reported beside the deciding full-history run)

- 405 / 405 cells have exec_mean_R > 0.
- PBO 0.307, Prob[OOS <= 0] 0.051, degradation slope -0.46: **PASS_PBO**.
- Winner (compound velocity): H6_E100_V4_PT1_SL1, exec mean +0.101 R (95 % CI +0.035 .. +0.165), 0.31 R/week, mean hold
  3.8 h.
- Under the standing rule (owner 2026-11-09) the full-history Gate C with the era-scaled spread DECIDES the lock and
  PBO / DSR. It is running (one batch, 5 workers). This 2022+ run supplies the sign check: the locked cell's 2022+
  exec mean must be > 0.

### XAUUSD Gate C -- DECIDING full-history run (era-scaled broker spread, 5,700 ms, 405 cells, stamp 20261109T150739Z)

- 405 / 405 cells have exec_mean_R > 0. Run time 59 min (one batch, 5 workers).
- PBO 0.140, Prob[OOS <= 0] 0.022, degradation slope -0.65: **PASS_PBO**.
- Winner / LOCK: **H8_E150_V4_PT1_SL1**. Exec mean +0.070 R (95 % CI +0.036 .. +0.105), 0.12 R/week, mean hold 3.7 h.
- DSR (Level 1, all 405 trials, E[K] ONC 8): **0.992** (> 0.95). PSR vs 0: 0.99996. FWER (report-only) passes at 5 %
  (alpha_K 0.0003).
- 2022+ sign check (plain broker model): locked cell +0.094 R (95 % CI +0.031 .. +0.154) > 0: **PASS**.
- Post-Gate C drift re-check at the locked cell (Rev 47, one-sided 90 %): full history drift +0.067 (z 2.90); 2022+
  +0.069 (z 1.70): **PASS** both (drift_recheck_locked_cell.json). C1 at the locked cell: z 2.71 / 1.51.
- Geometry locked 2026-11-09 in candidate_events.py (EVENT_VOL 8 h / EWM 150), model_rows.py (4 h, 1:1) and
  edge.yaml labeling.

Next: export_research_dataset -> Task 2A (executable targets) -> Steps 01-09 -> Gate D v4.1.

## XAUUSD/roundlevelcascade_long lifecycle after Gate C (2026-11-09; logs/xauusd_rlc_lifecycle_20261109/)

| Check | Result |
|---|---|
| Task 2A | 1,909 / 1,909 resolved; executable mean +0.075 R (median +0.105, 55.0 % positive); mid label mean +0.087 R |
| Steps 01-09 | ALL_STEPS_01_09_COMPLETE |
| Gate D v4.1 | **FAIL** -- D1: meta F1 0.510 vs take-every-trade F1 0.719 (bootstrap 5th pct -0.236); D2 PASS (log-loss gain +0.0062, lower bound +0.0011); AUC 0.52 (diagnostic) |
| Stress D079 (spread x 1.205, era-scaled) | PASS, +0.069 R; break-even spread about 8.9 x |
| CV leakage D080 | OK (purged AUC 0.51-0.53, shuffle inflation +0.002) |
| Label audit D081 | 1,909 rows, pos rate 0.561, mean uniqueness 0.919, effective N 1,754 |

Reading: the primary event is profitable after costs and robust to costs; the meta-model adds almost nothing (AUC 0.52),
so acting on it does worse than taking every signal. Under D087 item 3 (owner 2026-11-05) a Gate D fail may deploy
PRIMARY-ONLY (every signal, fixed size, no meta filter); that path is approved in principle but its design (sizing,
allocator) must be shown to the owner first. Not entered in edge_universe.

## D091 + promotion (2026-11-09)

- D090 post-Gate C re-check at the LOCKED cell H8_E150_V4_PT1_SL1 (gate0_T13_locked_verdict_d090.json): XAUUSD LONG
  **PASS** -- C1 diff +0.051 (z 2.71), C2 +0.047, 2022+ C1 +0.041, 3.02 events/week on 2022+.
- Owner 2026-11-09 (D091): Gate D is no longer a gate. Gate D v4.1 above is quoted as a diagnostic.
- **PROMOTED** to config/edge_universe.json (backup logs/edge_universe_before_gold_promotion_20261109.json); edge.yaml
  status deployability_pass. Next: allocator -> strategy risk (<= .10) -> live manifest -> Gate E -> Phase C/D -> owner go.
