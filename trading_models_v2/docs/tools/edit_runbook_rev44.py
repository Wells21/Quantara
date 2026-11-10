"""Quantara_Runbook.docx Revision 44 (2026-11-04): archive, test baseline and Gate D v4 decisions.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev44.py IN_DOCX OUT_DOCX
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
    hits = [m for m in PARA.finditer(ns["x"]) if key in html.unescape("".join(t[1] for t in TEXT.findall(m.group(0))))]
    if len(hits) != 1:
        raise SystemExit(f"paragraph count {len(hits)} for: {key[:80]}")
    m = hits[0]
    if len(TEXT.findall(m.group(0))) != 1:
        raise SystemExit(f"paragraph is not a single run: {key[:80]}")
    return m.start(), m.end(), m.group(0)


def set_para(key, new_text):
    s, e, p = _find(key)
    p2 = TEXT.sub(lambda t: t.group(1) + escape(new_text) + t.group(3), p, count=1)
    ns["x"] = ns["x"][:s] + p2 + ns["x"][e:]


def after_para(key, xml):
    e = _find(key)[1]
    ns["x"] = ns["x"][:e] + xml + ns["x"][e:]


def para_text(key):
    return html.unescape("".join(t[1] for t in TEXT.findall(_find(key)[2])))


def edit_para(key, old, new):
    t = para_text(key)
    if t.count(old) != 1:
        raise SystemExit(f"substring count {t.count(old)} in paragraph: {key[:60]} / {old[:60]}")
    set_para(key, t.replace(old, new))


ns["replace_once"]("Revision date: 4 November 2026 - Revision 43 (Revision 42: 3 November 2026;",
                   "Revision date: 4 November 2026 - Revision 44 (Revision 43: 4 November 2026; Revision 42: 3 November 2026;")
after_para("Revision 43 (4 November 2026): RECERT outcome and the edge-level gate architecture (owner).",
           P("Revision 44 (4 November 2026): the 4 removed edge folders are archived (owner: 'Now'); the known unit-test "
             "failures are restated for an empty edge universe (12.3); Gate D v4's D1 decision point approved, D2 "
             "weighting pending with the book's answer recorded (Step 12B). Appendix 61."))

edit_para("Deployable (config/edge_universe.json): 0.",
          "Folders kept in place pending owner review (Step 13 archive rule).",
          "Folders archived to C:\\Users\\Administrator\\Desktop\\Quantara_edge_archive_2026-11-04 (owner 2026-11-04, "
          "README.txt there).")

edit_para("Rev 43 (D078, docs/mldp/rules/GATE_D_V4_D078.md;",
          "OPEN before Gate D v4 gates an edge (owner, backlog D078): D2 must weight calibrator, base rate and loss "
          "consistently (unweighted vs AFML-weighted give opposite verdicts on the RECERT edges), and the D1 decision point "
          "(p > 0.5 acts on 74-100 % of events because the return-attribution weights correlate with the label).",
          "Rev 44: D1's decision point is APPROVED as 'act when the cross-fitted calibrated P(win) > b' (replacing raw p > "
          "0.5, which acted on 74-100 % of events because the return-attribution weights correlate with the label); code "
          "update pending with D2. OPEN before Gate D v4 gates an edge (owner, backlog D078): D2's weighting. The book is "
          "weighted throughout (AFML s.4.6 pp.68-69; Snippet 7.4 cvScore; s.9.4 pp.134-135: sample-weighted cross-entropy "
          "is 'the right ML performance metric ... for financial applications'; s.10.3 sizes from the classifier's "
          "probability), and whether the live calibrator is fitted weighted too.")

edit_para("Rev 29 / Rev 30: known failures until the RECERT retrain",
          "Rev 29 / Rev 30: known failures until the RECERT retrain -- 8 tests that compile the current live manifest into "
          "Phase C (test_phase_c_feature_contract 1, test_phase_c4_production_router 2, "
          "test_phase_c5_production_certification 2, test_tick_curation 3 errors) fail because 4 models select columns "
          "removed in Ch.18-19.",
          "Rev 44: known failures with an empty edge universe (2026-11-04, 570 tests, 7 failures + 20 errors) -- all from "
          "the stale 19 October live manifest and the 4 archived edge folders: test_phase_c_feature_contract 1, "
          "test_phase_c4_production_router 2, test_phase_c5_production_certification 2, test_tick_curation 1, "
          "test_phase_c_consumer_contract 3, test_phase_d_candidate_contract 1, test_phase_d_candidate_first 1, and the "
          "Phase D provider tests of the archived edges (dailyrangefade_long 4, drawdownhazard_short 4, rangestructure "
          "candidate 4, rangestructure primary 4; FileNotFoundError). Left failing by the owner (the edges are killed); "
          "they clear when a new edge universe gets a new live manifest and Phase C / D are rebuilt (Section 11B).")

appendix = (
    H1("61. Appendix: 2026-11-04 Archive, Test Baseline and Gate D v4 Decisions (Revision 44)")
    + BUL("Archive (owner: 'Now'): GBPJPY/dailyrangecompletionfade_long, HK/drawdownhazardcontinuation_short, "
          "JPN/rangestructure_long and XAUUSD/rangestructure_long were moved intact to "
          "C:\\Users\\Administrator\\Desktop\\Quantara_edge_archive_2026-11-04. That includes their source, artifacts, "
          "models, the RECERT step 1 geometry locks and the uncommitted T3 Gate C files. Their repository files are "
          "removed from git in the same commit.")
    + BUL("Tests: the owner chose to leave the archived edges' Phase D provider tests failing ('the edges are killed so "
          "why reverting the geometry locks'). The full suite with an empty universe has 27 failures / errors, all "
          "listed in 12.3.")
    + BUL("Gate D v4: D1's decision point is approved: act when the cross-fitted calibrated P(win) > base rate b. D2's "
          "weighting is pending; the book answer is recorded.\n"
          "  - Earlier recommendation reversed: I had recommended unweighted, but AFML is return-attribution weighted "
          "throughout (s.4.6, Snippet 7.4, s.9.4 pp.134-135, s.10.3).\n"
          "  - Still for the owner: whether the live calibrator, which is unweighted, is fitted weighted too.\n"
          "  - Gate D v4 gates no edge until this is settled (backlog D078).")
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
