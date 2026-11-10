"""Quantara_Runbook.docx Revision 30 (2026-10-31): the three owner-approved fixes from the Revision 29 review.

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev29.py, plus
para_text / set_para / sub_para, which find ONE paragraph by its plain (unescaped) text and rewrite its single text run.

usage: python edit_runbook_rev30.py IN_DOCX OUT_DOCX
"""
import html
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, BUL = ns["P"], ns["H1"], ns["BUL"]

PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
TEXT = re.compile(r"(<w:t(?: [^>]*)?>)(.*?)(</w:t>)", re.S)


def _find(key):
    """The one paragraph whose plain text contains key -> (start, end, xml)."""
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    m = hits[0]
    if len(TEXT.findall(m.group(0))) != 1:
        raise SystemExit(f"paragraph is not a single run: {key[:80]}")
    return m.start(), m.end(), m.group(0)


def para_text(key):
    return html.unescape(TEXT.search(_find(key)[2]).group(2))


def set_para(key, new_text):
    s, e, p = _find(key)
    p2 = TEXT.sub(lambda t: t.group(1) + escape(new_text) + t.group(3), p, count=1)
    ns["x"] = ns["x"][:s] + p2 + ns["x"][e:]


def sub_para(key, old, new):
    t = para_text(key)
    if t.count(old) != 1:
        raise SystemExit(f"'{old[:60]}' occurs {t.count(old)} times in: {key[:60]}")
    set_para(key, t.replace(old, new))


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


# -- header ----------------------------------------------------------------------------------------------------------
ns["replace_once"]("Revision date: 31 October 2026 - Revision 29 (Revision 28: 31 October 2026;",
                   "Revision date: 31 October 2026 - Revision 30 (Revision 29: 31 October 2026; Revision 28: 31 October 2026;")
after_para("Revision 29 (31 October 2026): full review of this runbook",
           P("Revision 30 (31 October 2026): the owner-approved fixes from the Revision 29 review -- the feature-schema "
             "validator accepts the feature_expansion.py convention (Step 6), the date-dependent live-bar test is fixed "
             "(12.3), stale register statuses D005 / D006 / D011 / D028 updated (D028's meta-model DSR test added to the "
             "RECERT, 16C); Appendix 47."))

# -- Step 6 -----------------------------------------------------------------------------------------------------------
set_para("model_input_schema.json missing = EXPECTED (Step 04B creates it). Rev 29: validate_edge_feature_schema.py",
         "model_input_schema.json missing = EXPECTED (Step 04B creates it). Rev 30: validate_edge_feature_schema.py accepts "
         "computed_by runtime.py::build_features or, when build_features calls it, the edge's own feature_expansion.py::"
         "add_v1_feature_expansion (the convention since 2026-09-11; 24 entries) and passes on all 6 edge schemas; any failure "
         "is real. Until the RECERT retrain, edge_contracts.py fails on HK dhc, JPN rs, XAU rs and XAU vc (their models select "
         "columns removed in Ch.18-19).")

# -- 12.3 -------------------------------------------------------------------------------------------------------------
set_para("Rev 29: known failures until the RECERT retrain -- 8 tests",
         "Rev 29 / Rev 30: known failures until the RECERT retrain -- 8 tests that compile the current live manifest into "
         "Phase C (test_phase_c_feature_contract 1, test_phase_c4_production_router 2, test_phase_c5_production_certification "
         "2, test_tick_curation 3 errors) fail because 4 models select columns removed in Ch.18-19. (Rev 30: the "
         "date-dependent test_live_bar_memory_authority test now freezes its clock and passes.) C5 cannot seal while any "
         "test fails, and never weaken a test to pass.")

# -- 16C --------------------------------------------------------------------------------------------------------------
sub_para("Rev 29 - the RECERT (the full list is config/mldp_implementation_backlog.json)",
         "with D057 reported, D048 re-tested with --approved-only and the Ch.16 T1-T4 tests re-run (D060);",
         "with D057 reported, D048 re-tested with --approved-only, the Ch.16 T1-T4 tests re-run (D060) and D028 tested "
         "(DSR of the meta-filtered strategy vs taking every primary signal; the owner judges the meta-model on it);")

# -- Appendix 47 ------------------------------------------------------------------------------------------------------
appendix = (
    H1("47. Appendix: 2026-10-31 Revision 30 Fixes")
    + BUL("Register (config/mldp_deviation_register.json): D005 superseded by D054 / D050-A (DSR trials = each edge's "
          "replayed Gate C trials, Level 1, plus the thesis count, Level 2; per-edge research_trial_ledger.jsonl files; the "
          "global config/research_trial_ledger.jsonl stays empty; no historical backfill -- older trial Sharpe ratios are "
          "not recoverable); D006 superseded by D050-A; D011 superseded by D051 / D054 / D055; D028 stays open, its "
          "approved test moved from Ch.14 to the RECERT. Backlog: D005 / D006 / D011 removed, D028 -> task_after_retrain.")
    + BUL("tests/test_live_bar_memory_authority.py: the periodic-threshold test now freezes utc_now_ts at 2026-07-03 12:00 "
          "UTC (the day after its fixture bars); it had failed since 2026-07-02 was older than ADAPTIVE_THRESHOLD_MAX_DAY_AGE_DAYS "
          "(120). The test's assertions are unchanged.")
    + BUL("shared/edges/validate_edge_feature_schema.py: accepts the edge's feature_expansion.py::add_v1_feature_expansion "
          "as computed_by when runtime.py's build_features calls it (the 2026-09-11 convention, commit 4b58127f). No pipeline "
          "step runs the validator, so its failure since 2026-09-11 went unnoticed; the schemas are unchanged "
          "(feature_schema.json stays uniform across edges).")
    + BUL("Kept outside the repo (owner): Desktop/Quantara_tools_backup_2026-10-22/feature_groups.py.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
ns["x"] = x[:i] + appendix + x[i:]

zin = ns["zin"]
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = ns["x"].encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
