# A5 — AFML Ch.10 Sizing Reference Hardening

## Status

`DONE`. Canonical reference hardened with fixture tests cross-checked
against an independent `scipy.stats.norm.cdf` reference; a real, unforced
duplicate implementation found and consolidated; the Quantara extension
confirmed separately named, separately tested, and never silently collapsed
into the canonical formula.

## What was found

`shared/sizing/afml_ch10_reference.py` (installed by Task 1) already
correctly implemented AFML Ch.10's exact formulas (binary:
`z=(p-1/2)/sqrt(p(1-p))`, `m=2*Phi(z)-1`; one-vs-rest:
`z=(p_tilde-1/K)/sqrt(p_tilde(1-p_tilde))`, `m=x*(2*Phi(z)-1)`), but its test
coverage (`tests/test_mldp_afml_ch10_reference.py`) only checked a handful
of qualitative properties (null probability gives zero size, side flips
sign), not exact values against an independent reference, and had no
one-vs-rest, validation/error-path, or end-to-end `get_signal` coverage.

More significantly: `shared/calibration/sigmoid_afml.py` -- the module that
implements Quantara's empirical break-even rescaling extension
(`payoff_aware_afml_components`, exactly the "Quantara payoff-adjusted
sizing" the blueprint's P0.5/D007 items refer to) -- had its **own,
independent, unimported copy** of the exact same canonical binary formula
(`afml_probability_bet_size`), differing only in its output clip range
(`[0,1]` there vs `[-1,1]` in the canonical reference, a deliberate and
correct difference given that function is only ever fed an already-
directional "effective probability" >= 0.5). This is a real, unforced
duplication -- unlike the Step 01/02 purge/embargo case (see
`docs/mldp/A3_PURGE_EMBARGO_VERIFICATION.md`), `sigmoid_afml.py` does not run
on Google Colab, so there was no operational reason for a second copy to
exist.

## What was done

1. **Consolidated the duplicate.** `sigmoid_afml.afml_probability_bet_size`
   now delegates to `shared.sizing.afml_ch10_reference.probability_to_signal`
   (called with `predicted_label=1.0, num_classes=2`, then clipped to
   `[0,1]` as before) instead of recomputing the same z/Phi formula.
   Verified numerically identical to the pre-consolidation behavior across
   200 points spanning `(0,1)` before treating this as safe.
2. **Hardened `tests/test_mldp_afml_ch10_reference.py`** (17 tests, up from
   4): exact-value cross-checks against `scipy.stats.norm.cdf` for both the
   binary and one-vs-rest formulas, monotonicity and boundedness properties,
   validation/error-path coverage (probability outside `(0,1)`,
   `num_classes<2`, invalid `primary_side`, non-broadcastable shapes), a
   concurrent-bet averaging fixture, and an end-to-end `get_signal`
   composition test (10.3 -> 10.4 -> 10.5).
3. **Added `tests/test_sigmoid_afml_sizing_extension.py`** (7 tests) with
   two deliberately distinct purposes:
   - Prove the consolidated `afml_probability_bet_size` matches the
     canonical reference exactly (200-point sweep plus hand-computed scipy
     cross-checks) -- this is the regression guard against the duplication
     silently reappearing.
   - Prove `payoff_aware_afml_components` (the named Quantara extension) is
     mathematically sound on its own terms (zero bet size exactly at the
     empirical break-even probability, near-maximal size at high confidence,
     break-even probability of 0.5 when the reward:risk ratio is 1:1) --
     and explicitly assert its output is **not** numerically identical to a
     naive canonical call at the same input probability, guarding against a
     future edit accidentally collapsing the extension back into the
     canonical formula and hiding the rescaling it exists to apply.

All 24 new/hardened tests pass. Existing downstream consumer test suites
(`shared/tests/test_v3_phase2_executable_payoff.py`,
`test_v3_phase3a_semantic_contract.py`, `test_v3_phase3b_live_payoff_contract.py`,
`test_v3_phase3c_semantic_generation_closure.py`) were re-run after the
consolidation and remain green (10 passed) -- confirming the internal
refactor did not change any observable behavior for existing callers.

## What is still open (out of scope for A5 itself)

Per the blueprint's P0.5, promotion of the Quantara extension over the
canonical reference (or vice versa) still requires an out-of-sample,
trial-count-corrected benchmark comparison (blueprint Phase 7/8) -- that is
downstream work for a later stage, not something A5's test-hardening pass
resolves. What A5 does establish: the two objects are now provably distinct,
both individually correct on their own terms, and neither can silently stand
in for the other without a test failing.
