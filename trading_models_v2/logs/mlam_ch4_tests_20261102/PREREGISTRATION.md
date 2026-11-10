# MLAM Ch.4 tests -- written before any result, 2026-11-02

Owner 2026-11-02: "Close Ch.3, commit and push, then MLAM Ch.4". Diagnostics only: nothing in code or any gate changes on
these results; findings go to the owner.

Book: MLAM Ch.4 pp.52-64 (pdf = book + 6). ONC = Snippets 4.1-4.2 (pp.56-59): k-means double loop over k = 2..N-1 and
n_init initialisations, quality q = E[S]/sqrt(V[S]) of the silhouette scores, re-cluster below-average clusters, accept if
the average quality improves. The book's initialisations are unseeded (random); ours are seeded (seed 0) for
reproducibility. Two implementations: shared/colab_portable/denoise_onc_snippet.py cluster_kmeans_top (correlation input;
backtest-trial E[K] for DSR/FWER, D076) and shared/features/clustered_feature_importance.py onc_top (observation-matrix
input; Step 01 feature clusters on the normalised VI matrix, PCA-reduced, p.55).

## T1 -- the book's Monte Carlo (s.4.5, Snippet 4.3, Figure 4.4)
Random block correlation matrices (Snippet 4.3 verbatim: sigma .5 blocks + sigma 1 one-block noise, minBlockSize M = 2),
rows/columns shuffled; N = 20, 40, 80 (160 omitted: cost), K = 3, 6, ... up to N/2; 20 generations per (N, K) (book: 1,000;
reduced for cost); both implementations (onc_top fed the book's observation matrix sqrt((1 - rho)/2)); n_init 10; seed =
generation index. Report E[K]/K quartiles by K/N bucket (Figure 4.4 buckets) and the adjusted share of exactly recovered
partitions. Reading rule: median E[K]/K in [0.98, 1.02] in every bucket for an implementation = it reproduces the book.

## T2 -- seed stability of the trial E[K] (DSR / FWER input)
The 4 all-trials records (same stamps as ch14_dsr_all_trials.json; D076 trial correlation: denoised + detoned), ONC seeds
0-9: E[K], DSR, FWER alpha_K. Reading rule: if every edge's DSR > 0.95 and FWER < 0.05 verdicts are identical across the
10 seeds, seed 0 is kept; otherwise the spread goes to the owner with a proposal (the book prescribes unseeded runs, so no
seed is "the book's").

## T3 -- seed stability of Step 01 feature clusters
For the 6 Gate C edges: Step 01's candidate feature matrix built exactly as the edge's 01_feature_surgery_clustered_mda.py
(load_model_rows, choose_candidate_features, NaN filter), observation matrix "vi" (Step 01's), ONC seeds 0-9: number of
clusters and the variation of information between each seed's partition and seed 0's (MLAM s.3.10 pp.46-47), normalised
by log(number of features). Clustered MDA is NOT re-run. Reading rule: normalised partition VI <= 0.10 for every seed =
stable; otherwise reported to the owner (Step 01 selection is per cluster, so unstable clusters can change selection).

Driver: shared/research/mlam_ch4_tests.py. Outputs here.

## T4 -- PCA-reduced observation matrix for the trial ONC (added 2026-11-02 before any T1-T3 result was available)
MLAM p.55: "For large matrices X, generally it is good practice to reduce its dimension via PCA" -- X replaced by its
standardised orthogonal projection on the number of dimensions = eigenvalues of X's correlation above lambda+. Step 01
does this; the trial ONC (406 x 406 X = sqrt((1 - rho)/2) of the D076 trial correlation) does not. For the 4 edges, seed 0:
E[K], DSR, FWER with X reduced exactly as Step 01 (clustered_feature_importance.observation_signal_dims /
_reduce_observations) and ONC (onc_top). Reading rule: same DSR / FWER verdicts as the unreduced D076 run = no change
proposed; a verdict change goes to the owner.

## T5 / T6 -- convergence of ONC with more initialisations (owner 2026-11-02: "Yes, run the convergence test in parallel")
Written after T1-T4 (results in walkthrough s.4Z.1), before any T5/T6 result.
T5 (Step 01): the same 6 edges, observation matrix and onc_top as T3, n_init = 50 and 100 (book Snippet 4.1 default 10),
seeds 0-4. Per n_init: number of clusters, partition VI vs that n_init's seed 0 (normalised by log N), silhouette q of the
returned partition. Reading rule: the smallest n_init at which every seed's normalised partition VI <= 0.10 on every edge
is proposed as Step 01's n_init; if neither 50 nor 100 converges, report and propose alternatives to the owner (none
chosen in advance).
T6 (trial ONC with the p.55 PCA reduction, as T4): the 4 edges, n_init 10 and 50, seeds 0-4: E[K], DSR, FWER. Reading
rule: identical DSR / FWER verdicts across seeds at an n_init = the reduction can be proposed at that n_init; otherwise
the unreduced D076 trial ONC (seed-stable, T2) stays.

## T7 -- unreduced D076 trial ONC at n_init 50 (added after T5/T6 results, before any T7 result)
Why: T6 showed the reduced trial ONC moving from fragmented partitions at n_init 10 (E[K] 2-18) to E[K] = 2 on every
seed at n_init 50, so the unreduced D076 E[K] (stable across seeds at n_init 10, T2) may also depend on n_init. The 4
edges, D076 trial correlation (unreduced), cluster_kmeans_top n_init 50, seeds 0-4: E[K], DSR, FWER. Reading rule:
verdicts identical to T2 (n_init 10) = n_init does not matter for the trial ONC; otherwise both are reported to the owner
with the T6 n_init 50 result.

## T8 -- does the remaining Step 01 partition variation change rule-B selection? (owner-approved 2026-11-02, after T5-T7,
before any T8 result)
Why: at n_init 50 (adopted, D077) the Step 01 cluster count is 2 on every seed, but cluster membership still differs
across seeds (normalised partition VI 0.13-0.21 > 0.10). What matters downstream is the kept feature set. The 6 edges,
the full Step 01 clustered importance (select_features_clustered: 1,000 trees, purged 10-fold, 5 MDA shuffles, rule B
z 1.645) with the ONC partition taken at n_init 50 and ONC seeds 0-4; tree / bootstrap / shuffle seed fixed at 0, so only
the partition varies. Per run: the kept feature set, n clusters, OOS AUC; check the partition equals the T5 n_init-50
partition at that seed. Reading rule: the kept feature set identical across the 5 ONC seeds on every edge = the residual
membership variation does not matter, n_init 50 is sufficient and Ch.4 can close; otherwise the per-edge differences
(features gained / lost, Jaccard vs seed 0) are reported to the owner with alternatives (none chosen in advance).

## T9 -- best-of-S restarts: does the max-q search converge, and is rule-B selection then stable? (owner approved option
A 2026-11-02, after T8, before any T9 result)
Option A: Step 01 runs ONC (n_init 50) at S seeds and keeps the partition with the highest silhouette quality q
(Snippet 4.1's objective; equivalent to n_init 50 x S). Stage 1: the 6 edges, n_init 50, ONC seeds 0-59 (seeds 0-4
from T5), q and partition per seed. Best-of-S for S = 5, 10, 20 on disjoint seed blocks (12 / 6 / 3 blocks). Per S and
edge: the share of blocks whose winning partition is the same partition (normalised partition VI <= 0.01 vs the winner
of the full 60-seed sweep). Stage 2: rule-B selection (as T8: trees/MDA seed 0) on every distinct block-winning
partition at S = 10 and S = 20. Reading rule: the smallest S at which every block on every edge wins the same partition
AND the same kept feature set is proposed as STEP01's S; if no S <= 20 achieves it, report the block-winner spread and
the selection spread to the owner (no fallback chosen in advance).
