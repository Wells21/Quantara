# T12 results (2026-11-07): Gate 0 FAIL both sides, every asset

Full history, pooled over the 5 equity indices:
- LONG (gap up accepted): n 1,674; drift +0.032 R (se 0.029; lower bound at 90 % -0.005) FAIL; C1 +0.092 (se 0.032)
  PASS; C2 +0.036 > 0.
- SHORT (gap down accepted): n 1,233; drift +0.025 (se 0.035) FAIL; C1 +0.045 (se 0.038) FAIL.

NASDAQ own result (the target):
- LONG: mean +0.002 R, drift -0.009 -> FAIL. In 2022+ the drift is -0.047.
- SHORT: drift +0.067 (2014-21 +0.045, 2022+ +0.097), C1 +0.077, C2 +0.150 -- all own diffs > 0. The pooled tests
  fail, so the verdict is FAIL.

Read:
- C1 passing on LONG means rejected gaps lose. The information is in the first 30 minutes' direction itself
  (opening-drive continuation), not in the gap being accepted. That would be a different thesis, found here, so it is
  a contaminated lead and not adopted (T8 precedent).
- JPN is strong on both sides (+0.07 / +0.20 drift), but its pooled test fails. It is recorded only.

Gate 0 tests in the NASDAQ campaign: 25, 0 passes.
