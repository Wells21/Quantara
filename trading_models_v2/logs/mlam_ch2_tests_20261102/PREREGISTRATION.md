# MLAM Ch.2 tests -- written before any result, 2026-11-02

Owner 2026-11-02: "then MLAM Ch.2". Diagnostics only: nothing in the code or any gate changes on these results; findings
go to the owner (book by default, deviations proposed first).

## T1 -- denoise / detone before clustering the backtest trials (DSR E[K], D054)

Book: MLAM s.2.1 p.24 "Throughout this Element, we assume that empirical covariance and correlation matrices have been
subjected to this procedure"; p.38 "Once a correlation matrix has been denoised and detoned, it can reveal important
structural information"; p.54 detone "if a strong common component is present"; p.87 "In a nonexperimental setting,
the researcher should denoise and detone the correlation matrix before clustering"; p.30 detone "if it exists (a
hypothesis that can be tested statistically)". Code today: shared/research/dsr_all_trials.py, portfolio_allocator/
07b_dsr_psr_validation.py and shared/research/allocator_threshold_pbo.py run ONC (clusterKMeansTop) on the RAW trial
correlation (walkthrough s.4N.1: "no denoising: the section does not ask for it").

Per edge with an all-trials record (GBPJPY drcf, HK dhc, JPN rs, XAUUSD rs; the same stamps as their
ch14_dsr_all_trials.json; all trials, D051): E[K], V[aSR] (getIVP extended, the owner's 2026-10-26 weighting), DSR,
FWER under
- V0 raw correlation (reproduces the stored record: same E[K] and DSR, else the test is void);
- V1 denoised (Snippets 2.4-2.5: findMaxEval bWidth 0.01, nFacts = count above lambda+ (Snippet 2.4 searchsorted, no
  floor), constant residual eigenvalue);
- V2 denoised then detoned (s.2.6, one market component removed) -- applied only if the market-component test passes:
  the first eigenvalue exceeds lambda+ AND >= 95 % of 1,000 iid row-bootstrap resamples (seed 0) give a first
  eigenvector with all loadings of one sign (the rule in force in Step 07 since 2026-10-30); reported either way.
ONC as stored: clusterKMeansTop, max k = N - 1, n_init 10, seed 0.

Reading rule (stated in advance): if E[K] and the DSR pass/fail verdicts are the same under V0, V1 (and V2 where it
applies), the book fix changes nothing material and is recommended as a code fidelity fix for the RECERT; if a verdict
changes, the change is presented to the owner with the numbers.

## T2 -- KDE bandwidth (bWidth) sensitivity of the Marcenko-Pastur fit

Book: Snippets 2.2/2.4 use bWidth = .01; footnote 8 p.35 and Exercise 7 p.37 leave cross-validated bWidth as an
exercise. Code: bWidth 0.01 in Step 07 and dsr; Step 01 signal dimensions 0.01. For the Step 07 6-edge correlation
(portfolio_allocator outputs) and the T1 trial correlations: nFacts and lambda+ at bWidth 0.005, 0.01, 0.02, 0.05, 0.1,
and at the bWidth chosen by 5-fold CV of the KDE log-likelihood (sklearn GridSearchCV over a log grid 0.001-1;
Exercise 7). Reading rule: if nFacts at 0.01 equals nFacts at the CV bandwidth, keep 0.01 (book example).

Driver: shared/research/mlam_ch2_tests.py. Outputs here.
