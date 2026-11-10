# Task 2B — Executable-Target Population Rule Decision

## Status

`RULE DRAFTED, TASK 2B.1 DIAGNOSTIC BUILT AND TESTED — STILL AWAITING SIGN-OFF
BEFORE TASK 2C`. Nothing in this document authorizes Step 01–09 to consume
`quote_executable_meta_label` for training. That authorization is a separate,
later action (Task 2C) once this rule is accepted and the symmetry check in
§4 is verified for every asset/edge, not only the pilot.

**Implementation status:** the §3 diagnostic and the three-way classifier are
now built and tested:
- `shared/execution/unresolved_event_disposition.py` — the classifier.
- `run_task2b_disposition_audit.py` — CLI that runs it against any edge's
  existing Task 2A resolution output.
- `tests/test_task2b_unresolved_disposition.py` — 20 unit tests covering
  every disposition branch synthetically (fixed-status lookups, weekend-gap
  arithmetic, blackout-window arithmetic, both ambiguous-status
  disambiguation rules, and the end-to-end annotate/summarize path). All
  pass.
- Run against the real `BTCUSD/absorption_long` pilot: 5,637 resolved, 2
  `TRUE_NO_FILL` (both `stale_entry_quote`), 0 `DATA_DEFECT`, audit `PASS`.
  This pilot's data happens not to contain any `missing_entry_quote`,
  `unresolved_tick_coverage`, or `vertical_deadline_bar_not_found` rows, so
  the ambiguous-case disambiguation logic is validated only synthetically so
  far, not yet against a real edge that actually exercises it — worth
  re-running this CLI against a wider set of edges before Task 2C sign-off,
  specifically looking for at least one edge that exercises each ambiguous
  branch for real.

## Why this decision exists

AFML and MLAM define labeling, meta-labeling, and calibration semantics for
events that *do* resolve. Neither book defines what to do with a historical
opportunity that has no timely, resolvable execution outcome — that is a
Quantara data/execution-realism question, not a book question. Task 2A's own
engine (`shared/execution/executable_target_dataset.py`) was deliberately built
audit-only for exactly this reason: it resolves every event it can, leaves the
rest unlabeled with a recorded reason, and refuses to authorize training until
one universal rule is frozen. This document is that freeze.

The governing principle, stated once so it does not get re-litigated per edge:

> **An event's disposition in the training population must match what the
> live execution bot would actually do when it meets the same condition.**
> If live trading would never generate an acted-on bet from a given
> condition (because the signal is too old, or no quote arrived in time),
> excluding that event from training is not survivorship bias or leakage —
> it is training on exactly the population the system will ever act on.
> If live trading *would* still act (or the condition is a pipeline defect
> rather than a market condition), the event must never be silently dropped.

This is the meta-labeling framework's own logic (AFML Ch.3 §3.6, p.50–51):
a meta-label only exists for a bet that was actually acted upon. An event
where the theoretical entry never received a live-eligible fill was never a
bet — the primary rule fired, but the secondary/execution layer never turned
it into a position, exactly as it wouldn't in production either.

## The three dispositions

Every one of Task 2A's `execution_status` values is assigned to exactly one
of three dispositions. No status is a hybrid.

### A. `TRUE_NO_FILL` — excluded from the training population, and this is correct, not a gap

The event's theoretical entry never became executable inside the same window
the live executor itself enforces. Training must never see this row as a
labeled outcome, because live trading never would have either.

- **`stale_entry_quote`** — the first available tick after the event arrived
  later than `max_signal_age_seconds` (120s). **Verified**:
  `portfolio_live/portfolio_execution_bot.py` already hard-codes
  `PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS = 120`, the same value the
  resolver uses. The symmetry condition in the governing principle is
  therefore already satisfied for this status without any further code
  change — confirmed by direct inspection, not assumed.
- **`missing_entry_quote` / `unresolved_tick_coverage`, sub-case "genuine
  market inactivity"** — no tick arrived for a period consistent with a
  known market closure (weekend, exchange holiday, a documented outage
  window). See §3 for how this sub-case is distinguished from the other two
  below, since the raw status code alone does not disambiguate it.

### B. `RIGHT_CENSORED_END_OF_HISTORY` — excluded from training, tracked and reported separately, never conflated with A

The event could not be resolved not because of a market condition, but
because Quantara's own collected history simply ends before the outcome
could be observed. This is a research-data-completeness artifact, not
something live trading will ever encounter (live always has a continuously
arriving tick stream), so it must not be pooled with the `TRUE_NO_FILL`
statistics — doing so would understate how often the *live* system actually
declines to trade, and would silently shrink as more history accrues in a
way that could be mistaken for an improving no-fill rate if not tracked
separately.

- **`vertical_deadline_bar_not_found`** — the event is close enough to the
  end of the available bar series that its vertical barrier deadline falls
  outside collected history. Always this disposition; there is no
  market-condition reading of it.
- **`missing_entry_quote` / `unresolved_tick_coverage`, sub-case "ran out of
  tick catalog"** — the resolver reached the last tick file actually present
  on disk with no more data to search, rather than encountering a genuine
  gap inside otherwise-continuous coverage. See §3.

### C. `DATA_DEFECT` — must not occur in a correct pipeline; hard-fail, never silently drop

These statuses indicate the resolver disagrees with its own inputs in a way
that should be structurally impossible if the gross model rows, the geometry
contract, and the tick catalog are all mutually consistent. A nonzero count
here is a signal that something upstream is broken (a stale geometry hash, a
tick-catalog gap that shouldn't exist, a corrupt bar) — coercing these to
"no label" would hide a real bug behind the same statistics that legitimately
explain `TRUE_NO_FILL`/`RIGHT_CENSORED_END_OF_HISTORY` events.

- **`event_bar_not_found`** — the event's own timestamp does not resolve to
  any bar. Should be impossible if the event came from this edge's own bar
  series.
- **`invalid_signal_mid`** — non-finite or non-positive bar close at the
  event bar. Indicates a bad bar, not a market condition.
- **`research_entry_signal_mid_mismatch`** — the gross pipeline's own
  recorded entry price disagrees with what this resolver independently
  computes from the bar series at the same timestamp. Indicates the gross
  model rows were built under a different geometry/bar contract than the
  one currently active — a version-drift bug, not something to average
  away.
- **`invalid_barrier_distance`** — non-finite or non-positive PT/SL
  distance (e.g. `Event_Vol` was zero or NaN at the event). Indicates an
  upstream feature/geometry defect.

**Rule: if any `DATA_DEFECT` status appears for an edge, that edge's audit
run must be treated as failed (equivalent to `--strict-coverage` failing),
investigated, and fixed at the source before that edge proceeds to Task 2C
training authorization — not silently excluded and moved past.** A `0` count
across all four `DATA_DEFECT` statuses is itself a pre-condition for Task 2C
sign-off, per edge.

## §3 — Disambiguating `missing_entry_quote` / `unresolved_tick_coverage`

These two status codes are structurally ambiguous as currently emitted: the
resolver cannot yet tell, from the status string alone, whether "no more
ticks were found" means (a) the market was genuinely closed, (b) the tick
catalog has an unexplained hole in the middle of otherwise-continuous
coverage, or (c) the catalog has simply reached its own most recent file.
This ambiguity must be resolved by a small diagnostic, not by a policy
guess, because guessing here is exactly the kind of invented semantics both
books warn against.

**Built as `shared/execution/unresolved_event_disposition.py` (Task 2B.1).**
One refinement was found while implementing this that the original draft
above did not distinguish, and the built classifier follows the refined
rule, not the paragraph above verbatim:

**`missing_entry_quote`** (no bet was ever taken — the entry itself never
filled) genuinely is an entry-side "would live have traded this at all"
question, so it is checked against the calendar first:
1. Weekend-gap or session-blackout window (using the *exact* live
   configuration, see below) → `TRUE_NO_FILL`.
2. Else, if the event is at or after the tick catalog's own latest collected
   coverage → `RIGHT_CENSORED_END_OF_HISTORY`.
3. Else — the catalog nominally has data covering a period after this event
   and still found nothing, and it isn't a known closure window — →
   `DATA_DEFECT`.

**`unresolved_tick_coverage`** is different in kind: entry already
succeeded (a bet *was* taken), and the only open question is whether an
*exit* outcome can be observed before the vertical deadline. A weekend
occurring mid-hold does not erase the bet or make it hypothetical the way a
weekend at entry time does — bar-based deadlines already account for
closures — so this status is **not** checked against the weekend/blackout
calendar at all, only against catalog coverage of the vertical deadline
itself:
1. If the vertical deadline is at or after the tick catalog's latest
   collected coverage → `RIGHT_CENSORED_END_OF_HISTORY`.
2. Else → `DATA_DEFECT`.

**Calendar source, resolved without building a new trading-calendar
system:** Quantara's live executor already enforces exactly this kind of
guard (`portfolio_live/portfolio_execution_bot.py`'s `market_entry_guard()`
— weekend-gap arithmetic, session-blackout windows, and a separate
broker-tick-freshness check). Rather than building and maintaining a second,
independent calendar for research, the classifier loads
`PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET` and
`PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET` directly from
`portfolio_live/live_settings.py` (the same file, loaded the same
`importlib.util.spec_from_file_location` way, that
`shared/execution/oof_exact_execution.py` already loads for the live
signal-age cap) — so entry-side calendar symmetry is guaranteed by sharing
the literal configuration, not by two hand-maintained copies drifting apart.
The weekend-gap and blackout-window arithmetic itself is reimplemented
standalone (pure functions, no MT5/state dependency) rather than importing
`portfolio_execution_bot.py`, since that module has real side effects at
import time (constructs live state-store/signal-feed objects, may require
the `MetaTrader5` package) that a read-only research diagnostic must not
trigger.

**Note — a related but distinct live gate is deliberately not modeled
here:** `PORTFOLIO_MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET` (30s default, 15s
for BTCUSD) guards broker-tick *freshness at the moment of order
submission* in live trading — a different concept from anything the
historical resolver can reproduce, since it is about the live broker feed's
real-time staleness, not about whether a historical tick existed near an
event. It is out of scope for this population-rule diagnostic.

## §4 — The symmetry check this whole rule depends on

The governing principle only holds if live execution actually refuses to act
under the same conditions research excludes. Status, now checked directly
against `portfolio_execution_bot.py`'s `market_entry_guard()` (the function
that gates every live order submission):

- **Signal-age cap — verified.** `PORTFOLIO_EXECUTION_MAX_SIGNAL_AGE_SECONDS
  = 120` matches the resolver's `stale_entry_quote` threshold exactly.
- **Weekend gap / session blackout — verified, and now guaranteed by
  construction**, not just checked once: the Task 2B.1 classifier loads
  `PORTFOLIO_WEEKEND_ENTRY_GUARD_BY_ASSET` /
  `PORTFOLIO_ENTRY_BLACKOUT_WINDOWS_UTC_BY_ASSET` from the same
  `live_settings.py` the live executor itself reads, so this cannot silently
  drift out of sync the way two independently-maintained calendars could.
- **No-quote-available — verified.** `market_entry_guard()` explicitly
  returns `False, "missing_broker_tick_timestamp:..."` when there is no
  current broker tick, which is the live-side analogue of the resolver's
  `missing_entry_quote`.
- **One residual, structural asymmetry, noted honestly rather than
  "fixed":** live additionally enforces
  `PORTFOLIO_MAX_BROKER_TICK_AGE_SECONDS_BY_ASSET` (30s default, 15s for
  BTCUSD) — a broker-feed *freshness* check distinct from the signal-age
  check, guarding against network/broker-side lag at the moment of order
  submission. A historical tick replay has no analogue for this (a
  historical tick file has no concept of "how stale was the feed when this
  price arrived" — the tick timestamp *is* the ground truth), so the
  resolver cannot and does not reproduce it. This means the `resolved`
  population may in principle include a small number of events that live
  would additionally have blocked on broker-feed lag specifically — an
  unavoidable limitation of historical replay, not a defect in this rule,
  and worth remembering as a residual gap between backtest and live rather
  than something to chase further.

## §5 — What this authorizes and what it does not

- **Authorizes:** treating dispositions A and B as "no label, recorded with
  reason" in any future Task 2B-conformant dataset — i.e., these rows are
  legitimately absent from the training set, and their counts/rates should
  be reported per edge as first-class audit statistics (not buried), since a
  high `TRUE_NO_FILL` rate is itself informative about an edge's real-world
  tradeability.
- **Does not authorize:** Step 01–09 to actually train on
  `quote_executable_meta_label` yet (that is Task 2C, gated on: this
  document being accepted, §3's diagnostic existing and having been run at
  least once per asset to confirm the A/B/C split is stable, §4's symmetry
  check passing, and the P0.2 target-architecture freeze from the blueprint
  being written into doctrine alongside this file).
- **Does not authorize:** silently coercing any unresolved row to a loss, to
  `0`, or to any other outcome. That remains explicitly forbidden, per Task
  2A's own original design intent.

## §6 — Registry classification

This population rule is a **`QUANTARA_EXTENSION`**, not a canonical AFML/MLAM
object — neither book defines quote-feed data-gap or right-censoring
semantics. It should be added to `config/mldp_method_registry.json` under
that classification once accepted, with `quantara_status:
"TASK2B_POPULATION_RULE_FROZEN"`, and the corresponding deviation-register
item (currently `OPEN_TASK2`/`OPEN` in `config/mldp_deviation_register.json`
for the target/calibration items this gates) updated to reflect that Task 2B
is now decided, pending the Task 2C authorization step above.
