# MLAM Ch.6 tests (pre-registered 2026-11-03, before any result)

Book: MLAM Ch.6 "Feature importance analysis", pdf 80-97 = printed pp.74-91. Driver: shared/research/mlam_ch6_tests.py.
Most of Ch.6 was audited and implemented with AFML Ch.8 (walkthrough s.4H: D038-D045; Step 01 = VI clusters, clustered
MDA + MDI, rule B, mwfl 0.05, 5 shuffles; D077 best-of-20 ONC). These tests cover what that audit did not.

## T1 Exercises 3 + 5 (p.91): the production Step 01 path on the book's synthetic data
Snippet 6.1 data (40 features: 5 I_, 30 R_, 5 N_; 10,000 rows) at sigmaStd 0.1 (book) and 0 (Ex.3), run through
`select_features_clustered` exactly as Step 01 calls it (VI observation matrix = Ex.5, best-of-20 ONC, Step 01 trees,
purged 10-fold with t1 = t0 (no overlap), embargo 0, rule B). Plus Ex.3 literally: book-exact unclustered MDI / MDA
(mlam_ch6_synthetic_replication functions) at sigmaStd 0.
Reading rule: PASS if (a) the ONC partition puts every R_ with its source I_ and all N_ apart from the I_/R_ clusters
(partition VI to the 6-group truth, MLAM s.3.10, reported), and (b) rule B keeps every I_/R_ feature and drops every N_.
A FAIL means Step 01 cannot be trusted to separate signal from noise even when the answer is known -> report.

## T2 Exercise 4 / s.6.4 (pp.82-83): probability-weighted accuracy as the MDA score
Step 01 on all 6 edges with the D077 partition held fixed (ONC computed once and cached), same folds, bootstrap draws and
shuffles; only the score changes: weighted PWA = sum w y_n (p_n - 1/2) / sum w (p_n - 1/2), p_n = max(p, 1 - p), and
importance (scr0 - scr1) / (1 - scr1) (AFML Snippet 8.3 form for a score bounded by 1). Rule B unchanged.
Reading rule: diagnostic. If the rule-B kept set is the same on every edge -> no change. If it differs, report the
clusters that flip with both t-stats; a change of score would be a deviation proposal (the book recommends log loss over
accuracy, s.6.4 p.82, and offers PWA as an easier-to-read alternative, not a replacement).

## T3 s.6.5.2 Step 1 (p.85): residual features under the D077 partition
D042 (2026-10-24) rejected residual features on the pre-D077 partitions. The book's condition: residualise when
silhouettes do not "clearly indicate that features belong to their respective clusters". Report each D077 cluster's
mean silhouette, then rule-B selection with cfi.residualize (cluster order as ONC returns it) on the same partition.
Reading rule: diagnostic for D042. If any cluster's mean silhouette < 0.25 AND the residual kept set differs from the
raw kept set on any edge -> report to the owner as a D042 re-open candidate; otherwise D042 stands.

Trials: T2/T3 are feature-selection diagnostics, logged as research trials per edge; no backtest is run.

## T3r (POST HOC, added 2026-11-03 after the HK and GBPJPY T3 results, labelled as such)
p.85 residualises cluster k only on clusters l < k, so with two clusters the second loses everything it shares with the
first. HK and GBPJPY both dropped exactly the second cluster. T3r repeats T3 with the cluster order reversed; it is read
only together with T3 (a cluster that is dropped in one order but kept in the other carries shared, not exclusive,
information).

## T1m (POST HOC, added 2026-11-03 after T1 sigma 0.1, labelled as such)
T1 sigma 0.1: VI + ONC recovered the 6-group truth exactly (partition VI 0) but rule B kept only 2 of 5 signal clusters
(clustered MDA 0.007-0.039 vs the book-exact 0.14-0.23; t 1.1-2.4). Suspected cause: D041 (importance trees
min_weight_fraction_leaf 0.05; Snippet 6.8 uses 0). T1m repeats T1 sigma 0.1 with mwfl 0, nothing else changed.
Reading: if rule B then keeps all 5 signal clusters and drops noise, D041 costs Step 01 its power on this data -> report.

## T4 (POST HOC, approved by the owner 2026-11-03 after T1m, written before any T4 result)
Step 01 on the 6 edges with the D077 partition held fixed (cached ONC), everything else unchanged, two arms: D041
(importance trees min_weight_fraction_leaf 0.05, production) and Snippet 6.8 (0). Report per edge the rule-B kept set,
cluster MDA t-stats, OOS AUC and weighted OOS log loss of each arm. The D041 arm must reproduce the T2 log-loss kept set.
Reading rule: if mwfl 0 keeps the same or more clusters on every edge AND, on at least 4 of 6 edges, its weighted OOS
neg log loss is not lower and its OOS AUC is not lower than the D041 arm -> propose replacing D041 with
Snippet 6.8's 0; otherwise D041 stands and Step 01's low power on the synthetic data is recorded as a known limitation.
