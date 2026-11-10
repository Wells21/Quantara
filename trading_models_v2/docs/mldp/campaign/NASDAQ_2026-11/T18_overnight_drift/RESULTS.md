# T18 results: overnight drift vs the cash session (Gate 0 test 31, run 2026-11-09) -- PASS (see caveats)

Card: PREREGISTRATION.md (written before any run). Cell H4_V16 (4 h Event_Vol, EWM 100, 16 h vertical, 1:1).
Firing rate on 2022+: about 3.8-4.9 a week per side per index.

| side | window | n | pooled drift (se, z) | pooled C1 (se, z) |
|---|---|---|---|---|
| LONG at close | full | 11,771 | +0.018 (0.013, 1.35) | +0.026 (0.011, 2.38) |
| | 2014-21 | 7,049 | +0.016 (0.93) | +0.032 (2.28) |
| | 2022+ | 4,722 | +0.016 (0.73) | +0.018 (1.00) |
| SHORT at open | full | 14,369 | +0.016 (0.012, 1.35) | +0.027 (0.011, 2.40) |
| | 2014-21 | 8,711 | +0.018 (1.18) | +0.032 (2.28) |
| | 2022+ | 5,658 | +0.006 (0.31) | +0.019 (1.04) |

| asset | LONG edge mean R / own drift / 2022+ drift | verdict | SHORT edge mean R / own drift / 2022+ drift | verdict |
|---|---|---|---|---|
| NASDAQ | +0.078 / +0.054 / +0.032 | **PASS** | -0.035 / +0.018 / -0.015 | FAIL (recency) |
| US500 | +0.031 / +0.008 / +0.014 | **PASS** | -0.026 / -0.004 / +0.005 | FAIL (own drift) |
| DAX | +0.043 / +0.014 / +0.016 | **PASS** | -0.019 / +0.006 / +0.002 | **PASS** |
| JPN | +0.030 / +0.001 / +0.001 | **PASS** | -0.012 / +0.018 / +0.014 | **PASS** |
| HK | +0.036 / +0.020 / +0.036 | **PASS** | +0.010 / +0.042 / +0.025 | **PASS** |

Caveats (honest):
- **One test, two faces.** The LONG edge is the SHORT side's C1 and vice versa (close entries vs open entries).
  Both sides measure one contrast, "the overnight beats the session", so they are not independent evidence.
- **Shorts mostly lose outright.** The SHORT edges pass because random shorts in rising indices lose more. Their raw
  mean R is negative except HK (+0.010). Gate A (after cost) decides tradability.
- **Weaker since 2022** (pooled z 0.73 / 0.31). JPN LONG's recency pass is +0.001.
- The pooled drift z of 1.35 clears 1.28 narrowly. This is test 31 of the campaign; DSR / PBO at Gates A-C charge for it.
- Overnight holds pay index CFD swap every night. Gate C's broker cost model charges it.

Next: edges <ASSET>/overnightdrift_long (NASDAQ, US500, DAX, JPN, HK) and <ASSET>/overnightdrift_short (DAX, JPN, HK)
-> Gate A (405 cells, D050-A).

## Gate A (2026-11-09; 405 cells, full history, broker mean spread; control parity PASS on all 8)

| edge | rows | cells net > 0 | median / max net R | own cell (H4_E100_V16 1:1) net | next |
|---|---|---|---|---|---|
| NASDAQ long | 1,687 | 405 | +0.072 / +0.154 | +0.075 | Gate C |
| US500 long | 1,313 | 389 | +0.026 / +0.097 | +0.015 | Gate C |
| DAX long | 3,041 | 313 | +0.024 / +0.085 | +0.019 | Gate C |
| JPN long | 2,474 | 76 | -0.022 / +0.035 | -0.015 | Gate C |
| HK long | 2,919 | 66 | -0.018 / +0.055 | -0.018 | Gate C |
| HK short | 2,939 | 7 | -0.036 / +0.010 | -0.037 | Gate C |
| DAX short | 3,052 | 0 | -0.032 / -0.004 | -0.050 | **KILLED Gate B** |
| JPN short | 2,095 | 0 | -0.049 / -0.006 | -0.060 | **KILLED Gate B** |

The DAX and JPN short folders are archived to Desktop\Quantara_edge_archive_2026-11-09. As expected from Gate 0, shorting the
cash session loses money after cost on most indices.

## Gate C (2026-11-09; full history + era-scaled broker spread decides, 2022+ plain broker = sign check)

| Edge | Full: cells > 0 | Full verdict | 2022+: cells > 0 | Note |
|---|---|---|---|---|
| US500 long | 0 / 405 (best -0.045 R) | KILLED | 0 / 405 (best -0.018 R) | no executable profit in any cell |
| DAX long | 11 / 405 | KILLED_PBO: PBO 0.089 but Prob[OOS<=0] 0.833; winner +0.003 R (CI -0.016..+0.022) | 95 / 405 (PBO 0.216, Prob 0.584 -> fails too) | the drift is real, but too small to pay the spread |
| NASDAQ long | 75 / 405 (capped era table) | KILLED_DSR: PBO 0.049, Prob[OOS<=0] 0.419 pass, but DSR 0.002 (PSR 0.937, ONC E[K] 8); winner H8_E200_V24_PT2_SL1 +0.039 R (CI -0.020..+0.099) | 13 / 405; locked cell +0.034 R (CI -0.049..+0.117, n 842) | drift real but too thin to survive 405 trials |
| JPN long | 0 / 405 (best -0.044 R; pre-cap table) | KILLED | 137 / 405 (best +0.040 R) | the cap lowers JPN factors only ~10-15 %, cannot lift 0/405 cells from -0.044 R; no rerun |
| HK long | 0 / 405 (best -0.017 R) | KILLED | 1 / 405 (best +0.002 R) | |
| HK short | 0 / 405 (best -0.005 R) | KILLED_GATE_C | 137 / 405 (best +0.065 R) | profitable only since 2022; full history decides |

T18 CLOSED 2026-11-09: all 6 edges dead at Gate C (the drift is real but does not pay a retail CFD spread over the full history).
