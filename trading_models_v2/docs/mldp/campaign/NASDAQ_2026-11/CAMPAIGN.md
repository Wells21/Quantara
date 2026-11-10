# Edge campaign: NASDAQ (started 2026-11-04)

Owner, 2026-11-04: "concentrate on 1 asset at a time, we must get at least one long edge that goes live and at least
one short edge that goes live on an asset ... design something really real." Asset approved: NASDAQ first, US500
second.

## Design principles (from the RECERT evidence)

1. **Beat drift, not zero.** On 2026-11-02 / 11-04, random same-side entries in the same months earned +0.07..+0.10 R
   on every asset, and our old triggers did no better (D075). A thesis must say WHEN within the month returns are
   better than average.
2. **The primary trigger carries the edge.** The meta-model never raised F1 (D028); Gate D v4.1 now requires it. Theses
   are chosen so that the trigger itself is the mechanism.
3. **A forced, non-informational loser.** Every card names who trades for reasons other than information (hedging,
   liquidity, rebalancing) and why that is predictable.
4. **Price- and calendar-observable, bar-level** (no microstructure theses; price-only data).
5. **Two-sided by construction.** One mechanism gives a long and a short.
6. **Counterfactuals test the mechanism, not the pattern.** They are pre-registered on the card before any data on the
   thesis is looked at.
7. **Order of work:** Gate 0 on all 10 assets first, because it is cheap and kills fast. Only a NASDAQ pass goes to
   Gates A-C and on. The edge-level gates (runbook Rev 43-45 KEY RULES) decide edge_universe; the allocator and the
   deployment gates decide live.

## Owner approvals (2026-11-04)

- **Asset order:** NASDAQ, then US500.
- **D083: Gate 0 cell H1_V1** (1 h lookback, EWM 100, 1 h vertical, 1:1) is allowed for intraday theses, pre-registered
  on the card.
- **D084: mechanism features**, added identically to every edge's feature_schema (uniformity rule):
  - the day's move up to the entry time;
  - realised volatility;
  - an options-expiry calendar (monthly third Friday; daily 0DTE);
  - days to month-end.
- **Source check before the cards** (done 2026-11-04, see below).

## Sources read (2026-11-04)

- **Baltussen, Da, Lammers & Martens (2021), "Hedging demand and market intraday momentum", JFE 142:377-403.**
  - Last 30 minutes predicted by the previous close -> last 30 minutes, over 60+ futures, 1974-2020; reverts over the
    next days; linked to gamma hedging by option market makers and leveraged ETFs.
  - NQ: coefficient 6.36, t 7.97, R2 4.10 %, out-of-sample R2 3.76 %.
  - "we do not consider transaction costs"; net Sharpe positive in S&P futures at one tick.
- **Gao, Han, Li & Zhou (2018), "Market intraday momentum", JFE 129:394-414.** SPY 1993-2013: the first half-hour
  (from the previous close) predicts the last half-hour; R2 1.6 %; stronger with high volatility and volume.
- **Harvey, Mazzoleni & Melone (WP, 14 January 2026), "The Unintended Consequences of Rebalancing".**
  - The 60/40 drift signals predict equity returns: -17 bps the next day when equities are overweight.
  - The Calendar signal's predictability "peaks in the last four days of the month".
  - The signal uses S&P 500 and 10-year Treasury futures, so our price-only version uses the equity leg alone.
- **Etula, Rinne, Suominen & Vaittinen (2020), "Dash for Cash", RFS 33(1):75-111.** Monthly payment-cycle liquidity
  needs move liquid markets around month-end (abstract only; paywalled).
- **Cliff, Cooper & Gulen (2008 WP), "Return differences between trading and non-trading hours: like night and day".**
  Index ETFs earn their returns overnight; QQQ overnight-minus-day about 0.18 % per day (secondary summaries only; to
  be confirmed before T3's card).
- **Correction:** Lou, Polk & Skouras (2019), JFE 134:192-213, is about cross-sectional strategy returns (momentum
  overnight, others intraday), not index overnight drift. It is not used as T3's source.

## Thesis slate (NASDAQ)

| id | thesis | sides | Gate 0 cell | status |
|---|---|---|---|---|
| T1 | session-close hedging flow (intraday momentum into the close) | long + short | H1_V1 | Gate 0 FAIL 2026-11-04 (pooled over 10 assets; C1/C2 pass on US indices) |
| T1b | T1 on the US-index universe (owner-ordered post-hoc re-test) | long + short | H1_V1 | Gate 0 FAIL at 95 %; owner applied the tiered 90 % drift bar post-hoc: **SHORT PASS** (lower bound +0.007) -> Gate A; LONG FAIL (-0.0012) |
| T1c / T1bc | T1 / T1b with the live-tradable entry (first bar at or after 15:00; owner-ordered post-hoc) | long + short | H1_V1 | Gate 0 FAIL 2026-11-04 both sides; T1b's pass came from the non-causal entry bar -> NASDAQ/closehedgingflow_short STOPPED after Gate A (best 1 h cell +0.006 R gross) |
| T2 | month-end rebalancing pressure (equity leg) | long + short | H4_V24 | Gate 0 FAIL 2026-11-04 (C1, C2 about 0: no mechanism) |
| T3 | overnight dealer-inventory reversal at the European open (Boyarchenko, Larsen & Whelan, RFS 2023) | long + short | H1_V1 | Gate 0 FAIL 2026-11-05 both sides (NASDAQ LONG mean -0.009 R; source covers ES only) |
| T4 | month-end payment-cycle liquidity, settlement-locked windows (Etula et al., RFS 2020) | long + short | H4_V24 | Gate 0 FAIL 2026-11-05 both sides (NASDAQ SHORT -0.114 R, wrong sign) |
| T5 | vol-target rebalancing flow into the close (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-05 both sides (NASDAQ L +0.002 R, S -0.025 R) |
| T6 | dealer gamma regime fade/follow, hourly (OUR thesis; ~2,500 events/side) | long + short | H1_V1 | Gate 0 FAIL 2026-11-05 both sides (SE 0.018; regime label irrelevant) |
| T7 | monthly options-expiry hedge cycle (OUR formalisation) | long + short | H4_V24 | Gate 0 FAIL 2026-11-05 both sides (post-expiry short -0.095 R, wrong sign) |
| T8 | scheduled-release de-risking (short) / re-risking (long), D086 calendar (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-05 both sides; pre-release short wrong sign (-0.254 R): contaminated lead, not adopted |
| T9 | release-reaction continuation, all 10 assets (OUR thesis) | long + short | H1_V4 | Gate 0 FAIL 2026-11-05 every asset; reactions REVERSED on 8/10 (contaminated lead, not adopted) |
| T10 | Friday weekend de-risking vs the week's crowd, 9 assets (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-05 every asset (NASDAQ LONG +0.074 R, own diffs > 0, pooled fails) |
| T11 | benchmark-fix pre-fix pressure reversal, XAUUSD / GBPJPY / USDMXN (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-05 (pooled C1 +0.03, lb -0.015; XAUUSD LONG own diffs all > 0; C2 placebo fails) |
| T12 | cash-open gap acceptance, 5 equity indices (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-07 (pooled drift +0.032 / +0.025 R, not significant; NASDAQ SHORT own diffs all > 0) |
| T13 | round-level stop-cascade continuation, all 10 assets (OUR thesis; Osler 2003/2005) | long + short | H1_V1 | Gate 0 PASS 2026-11-07 (XAUUSD, HK, JPN, BTCUSD LONG). HK, JPN KILLED_PBO (2022+). **XAUUSD Gate C PASS 2026-11-09** (full history, era-scaled: PBO 0.14, DSR 0.992, lock H8_E150_V4 1:1, +0.070 R; 2022+ +0.094 R; drift re-check PASS) -> export, Task 2A, Steps 01-09, Gate D running |
| T14 | high-vol liquidity-provision reversal, all 10 assets (OUR thesis; Grossman-Miller 1988, Nagel 2012) | long + short | H1_V1 | Gate 0 FAIL 2026-11-09 both sides, every asset (pooled drift z 0.55 / 0.92; SHORT C1 z 2.7 comes from low-vol rises continuing) |
| T15 | same-hour intraday periodicity, all 10 assets (OUR thesis; Heston-Korajczyk-Sadka 2010) | long + short | H1_V1 | Gate 0 FAIL 2026-11-09 every asset both sides (drift NEGATIVE z -2.75 / -4.54; C1 ~0: no periodicity; part clock-entry effect) |
| T16 | session hand-over continuation (Tokyo/London/NY), all 10 assets (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-09 every asset both sides (pooled drift z -0.81 / +0.17; C1 ~0) |
| T17 | Bitcoin perpetual-funding cycle (00/08/16 UTC), BTCUSD (OUR thesis) | long + short | H1_V1 | Gate 0 FAIL 2026-11-09 (SHORT drift z 1.21 < 1.28, C1 z 2.14; effect in 2017-21, ~0 in 2022+; LONG wrong sign) |
| T18 | overnight drift vs cash session, 5 equity indices (Lou-Polk-Skouras 2019; BLW 2023) | long (close) + short (open) | H4_V16 | Gate 0 PASS 2026-11-09 (Rev 47 + recency): LONG all 5 indices, SHORT DAX/JPN/HK; pooled drift z 1.35 both; shorts lose outright except HK; weaker 2022+ -> Gate A |
| T19 | Tokyo fix yen demand, GBPJPY (Ito-Yamada 2017; gotobi) | long (into fix) + short (after fix) | H1_V1 | Gate 0 FAIL 2026-11-09 (LONG wrong sign; SHORT C1 z 1.27 < 1.28 and hour-matched drift -0.002: time-of-day, not the fix) |
| T20 | gold Asia (SGE open) vs New York (COMEX open) session drift, XAUUSD (OUR thesis) | long (Asia) + short (NY) | H4_V4 | Gate 0 FAIL 2026-11-09 (LONG full z 2.83 but 2022+ drift -0.000 -> recency fail; SHORT drift z 0.82) |
| T21 | overnight risk premium in non-equity risk assets (T18 cross-asset coverage): GBPJPY, USOIL, BTCUSD, USDMXN | risk-on overnight + risk-off session | H4_V16 | Gate 0 FAIL 2026-11-09 every asset (pooled drift z 0.78 / -0.18; negative in 2022+) |
| T22 | weekly time-series momentum, all 10 assets (Moskowitz-Ooi-Pedersen 2012; Hurst-Ooi-Pedersen 2017) | long + short | H8_V24 | Gate 0 FAIL 2026-11-09 both sides, every asset (pooled month-matched drift z -3.3 / -3.1; C1 with-vs-against trend z 2.3 / 2.2: the gain is the monthly trend, which month matching removes) |

Stop rule for NASDAQ: one live long and one live short, or the slate exhausted (every attempt logged and counted).


Owner 2026-11-04: tiered drift bar adopted (D075); stay with the ~4 years of data (no space for 10).
Owner 2026-11-04: the 2000 model-row floor is removed (D085, runbook Rev 46). T1b SHORT
(NASDAQ/closehedgingflow_short, 451 events at most on ~4 years) goes through the full pipeline; the statistical gates decide.

## History extension (2026-11-07)

Bars rebuilt on the longer history (NASDAQ from 2014-02; parser bugs fixed). Owner 2026-11-07: "don't reset" -- trials
accumulate. R1 (R1_history_extension_rerun/): 10 cards re-run unchanged as NEW trials under the Rev 47 rule + recency.
0 passes. T5 LONG passes on the full history but the effect is 2014-2021 only (decayed in 2022+). Gate 0 tests: 24.
T12 (test 25) FAIL. Rebuilt FFD/HMM/entropy verified 2026-11-07 (whole-second bars kept, ADF full p < 0.05, HMM converged).
T13 = test 26 (LONG PASS on XAUUSD, HK, JPN -> Gate A 2026-11-09, owner "yes approve both"). T14 = test 27 (FAIL). T15 = test 28 (FAIL). T16 = test 29 (FAIL). T17 = test 30 (FAIL). T18 = test 31 (PASS). T19 = test 32 (FAIL). T20 = test 33 (FAIL). T21 = test 34.
Owner 2026-11-09: Claude has the wheel -- search until 1 long + 1 short edge on EACH of the 10 assets; every edge must fire >= 3 times/week (STANDING).

**D090 (owner 2026-11-09, IN FORCE):** Gate 0 is decided per asset by the drift-neutral main counterfactual (one-sided 90 %), the other counterfactuals (> 0), C1 recency on 2022+ and >= 3 events/week; month-matched drift and pooled results are diagnostics (docs/mldp/rules/THEORY_CHECK_RULE_D075.md, D090 section). Stored results re-read read-only (gate0_*_verdict_d090.json beside each card): POST-HOC passes, inside each card's universe -- T13 XAUUSD LONG (already built); T16 USDMXN LONG; T17 BTCUSD SHORT; T18 NASDAQ LONG/SHORT, HK LONG/SHORT; T20 XAUUSD LONG/SHORT; T21 BTCUSD LONG/SHORT; T22 BTCUSD LONG/SHORT. They go to Gate A only by owner order. XAUUSD/roundlevelcascade_long: Gate D v4.1 FAIL (D1), all other edge-level checks pass -> primary-only candidate (D087 item 3, design pending).

**2026-11-09 (later):** owner ordered the D090 post-hoc passes to Gate A -- 9 edges: NASDAQ/overnightdrift_short, XAUUSD/goldsessionflow_long + _short (T20), BTCUSD/overnightriskpremium_long + _short (T21), BTCUSD/fundingcycle_short (T17), BTCUSD/weeklymomentum_long + _short (T22), USDMXN/sessionhandover_long (T16). Triggers in shared/edges/thesis_triggers.py; event parity with Gate 0 IDENTICAL on all 9 (docs/tools/check_thesis_trigger_parity.py). Queue logs/d090posthoc_gateA_20261109.sh. D091 (owner): Gate D is not a gate -> XAUUSD/roundlevelcascade_long PROMOTED (first edge in edge_universe since the RECERT).

| T23 | domestic-hours currency weakness, GBPJPY (Ranaldo 2009 JBF; Breedon-Ranaldo 2013 JMCB) | long (Tokyo open) + short (London open) | H4_V6 | Gate 0 test 36 (D090): FAIL both sides (C1 z -0.33 / -0.35) |

Gate A of the 9 D090 post-hoc edges (2026-11-09, logs/d090posthoc_gateA_20261109.sh; docs/tools/gate_a_summary.py): -> Gate C: XAUUSD/goldsessionflow_long (best gross +0.072, 405/405 net > 0), BTCUSD/overnightriskpremium_long (+0.083, 366), BTCUSD/overnightriskpremium_short (+0.074, 294), BTCUSD/fundingcycle_short (+0.085, 343). KILLED Gate A (best gross < 0.05): NASDAQ/overnightdrift_short (+0.005), XAUUSD/goldsessionflow_short (+0.025), USDMXN/sessionhandover_long (+0.029); archived to Desktop\Quantara_edge_archive_2026-11-09. BTCUSD/weeklymomentum_long/_short still in Gate A.
| T24 | volatility-managed premium, 5 equity indices (Moreira-Muir 2017 JF) | long (low vol) + short (high vol) | H4_V24 | Gate 0 test 37 (D090): PASS NASDAQ L+S (z 1.39 / 1.40, one contrast); FAIL US500, DAX, JPN, HK -> NASDAQ to Gate A |

| T25 | 52-week-high anchor, 5 equity indices (Li-Yu 2012 JFE; George-Hwang 2004 JF) | long (near) + short (far) | H4_V24 | Gate 0 test 38 (D090): PASS HK L+S (z 2.16 / 2.15, one contrast); FAIL NASDAQ, US500, DAX, JPN -> HK to Gate A |

| T26 | realized skewness / lottery demand, all 10 assets (Amaya et al. 2015 JFE; our time-series extension) | long (low skew) + short (high skew) | H4_V24 | Gate 0 test 39 (D090): FAIL every asset; BTCUSD / XAUUSD opposite sign (not adopted) |

BTCUSD/weeklymomentum_long (best gross +0.153, 296 cells net > 0) and _short (+0.101, 382) -> Gate C (logs/d090posthoc_gateC_wm_20261109.sh).

Gate C (2026-11-10, logs/d090posthoc_gateC_20261109.sh, full history with era-scaled broker spread decides, D088): BTCUSD/fundingcycle_short 0/405 cells positive (best -0.016; 2022+ 50 positive, best +0.009), BTCUSD/overnightriskpremium_long 0/405 (best -0.074; 2022+ 0), BTCUSD/overnightriskpremium_short 0/405 (best -0.017; 2022+ 329 positive, best +0.092) -> all three **KILLED_C** (no positive cell, so no PBO candidate set); archived to Desktop\Quantara_edge_archive_2026-11-09. XAUUSD/goldsessionflow_long: 298/405 positive full history (best H2_E100_V2_PT2_SL1 +0.024), 400/405 on 2022+ (best +0.138) -> PBO.
BTCUSD/weeklymomentum_long: full history 16/405 positive (best +0.051), PBO 0.074 but Prob[OOS <= 0] 0.574 -> **KILLED_PBO**. BTCUSD/weeklymomentum_short: 2/405 positive (best +0.004), PBO 0.700, Prob[OOS <= 0] 0.991 -> **KILLED_PBO** (2022+ also 0 positive). Both archived to Desktop\Quantara_edge_archive_2026-11-09. All 5 BTCUSD D090 post-hoc edges are dead at Gate C.
Gate C (logs/t2425_gateC_20261110.sh): HK/highanchor_long 0/405 positive full history (best -0.017; 2022+ 7, best +0.027) -> **KILLED_C**, archived. HK/highanchor_short 1/405 (best +0.004; 2022+ 132, best +0.108) and NASDAQ/volmanagedpremium_long 118/405 (best H8_E200_V96_PT2_SL1 +0.047; 2022+ 149, best +0.039) -> PBO.
PBO: HK/highanchor_short PBO 0.065 but Prob[OOS <= 0] 0.751 (winner +0.004, CI -0.083..+0.084) -> **KILLED_PBO**, archived; HK has no surviving T25 edge. NASDAQ/volmanagedpremium_long PBO 0.017, Prob[OOS <= 0] 0.255, slope -0.61 -> **PASS_PBO**; winner H8_E200_V24_PT2_SL1 +0.039 R (CI -0.011..+0.088), 0.35 R/week -> DSR / stress / D090 locked re-check.
XAUUSD/goldsessionflow_long post-PBO (logs/xauusd_gsf_lifecycle_20261110/): locked H8_E150_V4_PT2_SL1; 2022+ sign check +0.040 R PASS; stress x1.205 +0.020 R PASS (break-even about 3.0x); D090 re-check at the locked cell PASS (C1 z 2.39, 2022+ C1 +0.073, 4.8/week); **DSR 0.667 < 0.95 (E[K] ONC 6, selected SR rank 35 of 405, annualised SR 0.60 vs SR* 0.50) -> KILLED_DSR**. Archived.
| T27 | frog in the pan: continuous vs discrete information, all 10 assets (Da-Gurun-Warachka 2014 RFS; our time-series extension) | long (continuous up-week) + short (continuous down-week) | H4_V24 | Gate 0 test 40 (D090): PASS US500 LONG (z 2.86), XAUUSD SHORT (z 2.45), USOIL LONG (z 1.42); 17 FAIL (BTCUSD LONG recency); ~2 passes expected by chance -> Gate A |
NASDAQ/volmanagedpremium_long post-PBO (logs/nasdaq_vmp_lifecycle_20261110/): locked H8_E200_V24_PT2_SL1; 2022+ sign check +0.030 R PASS; stress x1.205 +0.036 R PASS (break-even about 3.3x); D090 re-check at the locked cell PASS (z 1.55, 10.8/week); **DSR 0.889 < 0.95 (E[K] ONC 2; PSR vs 0 only 0.926; FWER fails at 5 %) -> KILLED_DSR**. Archived. T24 has no surviving edge.
| T28 | Halloween season (Sell in May), equity indices (Bouman-Jacobsen 2002 AER) | long (Nov-Apr) + short (May-Oct) | H4_V24 | Gate 0 test 41 (D090): PASS DAX L+S (z 1.52; season-clustered t 1.80), HK L+S (z 2.17; season t 2.48); FAIL NASDAQ, US500, JPN; placebo XAUUSD strongest (z 2.70, red flag) -> Gate A |
T27 Gate A (logs/t27_gateA_20261110.sh): US500/continuousinfo_long best gross +0.156, 389/405 net > 0 (median net +0.057); USOIL/continuousinfo_long +0.069, 116; XAUUSD/continuousinfo_short +0.053, 405 -> all three to Gate C (logs/t27_gateC_20261110.sh).
T28 Gate A (logs/t28_gateA_20261110.sh): DAX/halloweenseason_long best gross +0.119, 291/405 net > 0; HK/halloweenseason_long +0.134, 244; HK/halloweenseason_short +0.116, 108 -> Gate C (logs/t28_gateC_20261110.sh). DAX/halloweenseason_short best gross +0.013, 0 net > 0 -> **KILLED_A**, archived.
T27 Gate C (logs/t27_gateC_20261110.sh): US500/continuousinfo_long full 57/405 positive (best +0.025; 2022+ 68, best +0.068); USOIL/continuousinfo_long 133 (best +0.047; 2022+ only 1, best +0.009); XAUUSD/continuousinfo_short 374 (best +0.040; 2022+ 264, best +0.110) -> PBO.
T27 PBO: US500/continuousinfo_long PBO 0.075, Prob[OOS <= 0] 0.548; USOIL/continuousinfo_long PBO 0.471, Prob 0.652; XAUUSD/continuousinfo_short PBO 0.643, Prob 0.523 -> all three **KILLED_PBO**, archived. T27 has no surviving edge.
| T29 | 12-month time-series momentum, all 10 assets (Moskowitz-Ooi-Pedersen 2012 JFE) | long (12m up) + short (12m down) | H4_V24 | Gate 0 test 42 (D090): PASS HK L+S (z 1.56; quarter t 1.25), XAUUSD L+S (z 2.00; quarter t -1.16, opposite sign: not credible); 16 FAIL -> Gate A |
T28 Gate C (logs/t28_gateC_20261110.sh): HK/halloweenseason_long 0/405 positive full history (best -0.020; 2022+ 0) -> **KILLED_C**, archived. HK/halloweenseason_short 34/405 (best +0.052; 2022+ 138, best +0.134) -> PBO 0.000, Prob[OOS <= 0] 0.390, slope -0.49 **PASS_PBO**; lock H8_E150_V168_PT2_SL1 (+0.052 R, CI -0.036..), 2022+ locked +0.132 R PASS. DAX/halloweenseason_long 42/405 (best +0.043; 2022+ 147, best +0.076) -> PBO 0.006, Prob 0.216, slope -0.78 **PASS_PBO**; lock H8_E200_V24_PT2_SL1 (+0.037 R, CI -0.021..), 2022+ locked +0.056 R PASS. -> DSR / stress / D090 locked re-check.
T29 Gate A (logs/t29_gateA_20261110.sh): HK/trendyear_long best gross +0.132, 227 net > 0; HK/trendyear_short +0.088, 85; XAUUSD/trendyear_long +0.088, 376 -> Gate C (logs/t29_gateC_20261110.sh). XAUUSD/trendyear_short +0.036 -> **KILLED_A**, archived.
T28 post-PBO: HK/halloweenseason_short stress +0.051 R PASS, D090 locked re-check PASS (z 2.50), **DSR 0.00004 (E[K] 9, selected aSR 0.44 vs SR* 1.58; PSR vs 0 0.940) -> KILLED_DSR**. DAX/halloweenseason_long stress +0.032 R PASS, D090 locked re-check PASS (z 2.28), **DSR 0.625 (E[K] 4, aSR 0.43 vs SR* 0.34; PSR vs 0 0.930) -> KILLED_DSR**. Both archived. T28 has no surviving edge.

**D092 (owner 2026-11-10, IN FORCE):** firing rate lowered to >= 2 edge events/week on 2022+ (was >= 3, D089); runbook Rev 49. Stored D090 verdicts re-read read-only; the firing rate does not select on returns, so the six pairs that failed only on it go to Gate A: T1c DAX LONG 2.56/wk (C1 z 2.48) + SHORT 2.28/wk (z 2.48); T13 XAUUSD SHORT 2.35/wk (z 2.31); T22 HK LONG 2.28/wk (z 1.86) + SHORT 2.52/wk (z 1.79); T22 XAUUSD SHORT 2.45/wk (z 2.57).

**D093 (owner 2026-11-10, IN FORCE):** Gate A/C grid = 120 cells (lookbacks 1,2,4,6,8 h x EWM 150 x verticals 1,2,4,8,16,24,48,96 h x PT:SL 1:1,1.5:1,2:1); canonical shared/research/gate_a_grid.sh; runbook Rev 50. Every cell still searched and paid (D050-A). No per-card holding-period band. T29 keeps its 405-cell run; the six D092 pairs run on 120.
T29 Gate C (full history, era-scaled; 405 cells): HK/trendyear_long 0 cells > 0 (best -0.002 R) -> KILLED_C, archived; HK/trendyear_short 3 cells > 0 (best +0.007); XAUUSD/trendyear_long 14 cells > 0 (best +0.019) -> PBO (logs/t29_pbo_20261110.sh).
D092 edges built 2026-11-10 (Gate 0 parity IDENTICAL for all six): HK/weeklymomentum_long + _short, XAUUSD/weeklymomentum_short (T22); XAUUSD/roundlevelcascade_short (T13, from the long's code, side flipped, provisional H1_E100_V1; 1808/1808 events); DAX/closehedgingflow_long + _short (T1c, new trigger t1c in shared/edges/thesis_triggers.py; card T1c is itself an owner-ordered post-hoc re-test). Gate A on the D093 120-cell grid: logs/d092_gateA_20261110.sh.
T29 PBO (full history): HK/trendyear_short PBO 0.009, Prob[OOS<=0] 0.710 -> KILLED_PBO, archived. XAUUSD/trendyear_long PBO 0.013, Prob[OOS<=0] 0.475 -> PASS; lock H8_E200_V96_PT2_SL1 (+0.019 R full, 2022+ +0.093 R) -> DSR, stress, D090 re-check (logs/xauusd_trendyear_lifecycle_20261110/post_pbo.sh).
