# MLAM Ch.7 tests (pre-registered 2026-11-03; written before any result except T1a and one timing simulation)

Book: MLAM Ch.7 "Portfolio construction", pdf 98-110 = printed pp.92-104. Audit: walkthrough s.4ZC. Drivers:
shared/research/mlam_ch7_tests.py (synthetic, venv312) and shared/research/mlam_ch7_ours.py (our inputs, venv39).
All diagnostic: nothing in the allocator or live changes. HRP stays under D060 unless T3's rule below is met AND a later
owner-approved M1 run (s.4P.1 rule) confirms it.

Disclosed before results: T1a (exact arithmetic, PASS) ran first. One timing simulation (book DGP, sim 0, 585 s) was
also run; it showed NCO below Markowitz in every cell for that draw. Its file was deleted, and it is re-run as sim 0 below.

## T1a Snippets 7.1-7.2 and the s.7.5 numbers
Condition numbers of the block matrices on pp.96-97: 3, 4 (blocks of 3 and 1), 7 (rho .75), 251 (500 x 500, two
blocks), and 3 (Snippet 7.2 dominant block). PASS = exact to 1e-9.

## T1b Figures 7.2-7.3 (s.7.7)
DGP Snippet 7.7: 10 blocks x 50, rho .5, seed 0. Snippet 7.8 draws: seed 0, nObs 1,000, one multivariate-normal
draw per simulation in the book's order. For each draw:
- raw covariance and Ledoit-Wolf covariance (sklearn LedoitWolf, as simCovMu);
- Markowitz optPort vs optPort_nco (maxNumClusters N/2 = 250, n_init 10);
- min-variance and max-Sharpe from the same draws (the clustering does not use mu);
- RMSE over all weights vs the true weights (Snippet 7.9).

Simulations: **100, not the book's 1,000.** The cost is 585 s per simulation for the five NCO clusterings of a 500 x 500
matrix, so 1,000 would take about 27 hours on 6 lanes. RNG: the book's KMeans drew from the global numpy state between
simulations, so its exact figures cannot be reproduced; our KMeans is seeded per simulation (seed = simulation index).

Reading rule: **replicated** if NCO's RMSE is below Markowitz's in all four cells (raw / shrunk x min-var / max-Sharpe),
which is the book's claim. Also reported:
- NCO / Markowitz ratios vs the book's .530 and .452;
- whether shrinkage lowers Markowitz's RMSE (erratum 2);
- Markowitz raw min-variance RMSE vs 7.95E-03 (Fig. 7.2) and 4.95E-03 (Ch.2 Fig. 2.5).

## T2 Exercises 1-5 (p.104), same draws as T1b unless stated
- **Ex.1:** Snippet 7.3 denoising (deNoiseCov, q = nObs/N, bWidth .01). NCO clusters the denoised corr, and separately
  the denoised + detoned corr (1 market component). Weights use the denoised cov (Snippet 7.4). Compared with NCO raw;
  Markowitz on the denoised cov is reported alongside.
- **Ex.2:** DGP getRndCov(500, 10) (Snippet 2.3, no block structure), seed 0; mu by formTrueMatrix's rule
  (N(std, std)); 100 simulations; Markowitz vs NCO raw, min-var and max-Sharpe.
- **Ex.3:** minVarPort replaced by long-only CLA solutions (0 <= w, sum 1, solved as the equivalent QPs with cvxpy
  Clarabel). Min-variance = the last turning point; max-Sharpe = min w'Vw s.t. mu'w = 1, w >= 0, rescaled. Markowitz-CLA
  vs NCO-CLA, both against the true long-only CLA weights.
- **Ex.4:** N = 10 (2 blocks x 5) and N = 100 (10 blocks x 10), rho .5, 1,000 simulations each, Markowitz vs NCO
  raw. Read with the N = 500 result as the ratio vs N.
- **Ex.5:** maxNumClusters = 2 on the T1b raw draws, vs NCO (N/2) and Markowitz.

Reading: the exercises are answered from the numbers; no pipeline effect.

## T3 our inputs (fn 24 p.101; s.7.6.1 fn 23 corr vs abs(corr); s.7.6.2 / s.7.8 NCO around other allocators)
Edges: the 4 deployable edges of config/edge_universe.json. Returns: daily mark-to-market R (alignment E, D059),
built as in ch16_allocation_tests. Min-variance only; max-Sharpe was rejected under D060.

Methods compared:
- HRP-current, IVP, Markowitz, and CLA long-only min-variance;
- NCO with min-variance on corr;
- NCO with min-variance on abs(corr);
- NCO with IVP intra and inter;
- NCO with long-only CLA intra and inter.

Design: the s.4P.1 M2 design, 1,000 Gaussian simulations at nObs 260 and T, seed 0. Each simulation reports RMSE vs the
true min-variance weights and own-RMSE (vs the method's own weights on the true V). Also reported: the correlation
matrix, the condition number, the minimum off-diagonal correlation, and the clusters on corr vs abs(corr).

Reading rule: an NCO variant is a **candidate** only if its own-RMSE is below HRP-current's at both nObs. A candidate
goes to the s.4P.1 M1 run (owner approval) before any proposal. Otherwise D060 stands (HRP).

## Scope change (owner 2026-11-03, before any T1b / T2 / T3 result was read)
Owner: "Yes, go ahead, run only T3". T1b and T2 (synthetic, they test the book, not Quantara; about 4 hours) were stopped
after about 15 minutes, with no chunk finished and no result read. The exercises are answered from the book's text and
the s.4P results instead. T1a (already PASS) and T3 stand as written.
