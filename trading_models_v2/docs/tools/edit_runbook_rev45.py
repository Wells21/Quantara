"""Quantara_Runbook.docx Revision 45 (2026-11-04): Gate D v4.1 and the weighted live calibrator (D082).

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev45.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 4 November 2026 - Revision 44 (Revision 43: 4 November 2026;",
                   "Revision date: 4 November 2026 - Revision 45 (Revision 44: 4 November 2026; Revision 43: 4 November 2026;")
after_para("Revision 44 (4 November 2026): the 4 removed edge folders are archived",
           P("Revision 45 (4 November 2026): Gate D v4.1 in force -- D1 acts when the cross-fitted calibrated P(win) > the "
             "weighted base rate, D2 is weighted throughout (owner, AFML s.4.6 / Snippet 7.4 / s.9.4 / s.10.3) -- and the "
             "Step 04 live calibrator is fitted with the AFML return-attribution weights (D082). Section 6A Steps 10F and "
             "12B, KEY RULES; Appendix 62."))

set_para("Rev 43 (D078, docs/mldp/rules/GATE_D_V4_D078.md;",
         "Rev 45 (D078 v4.1, docs/mldp/rules/GATE_D_V4_D078.md; AFML s.3.7 pp.52-53, s.14.8 pp.206-207, s.4.6 pp.68-69, "
         "Snippet 7.4, s.9.4 pp.134-135, s.10.3): on the Step 03 OOF rows, no Step 04 or allocator threshold. Cross-fitted "
         "calibration shared by D1 and D2: the live calibrator's method (Platt on stacked CPCV paths WITH the AFML "
         "return-attribution weights, D082) fitted on 9 of 10 purged + embargoed time blocks and applied to the 10th; b = "
         "the weighted base rate of the same training rows. D1 act or pass: F1 of acting when the calibrated P(win) > b "
         "minus the primary model's F1 (2b / (1 + b)), unweighted counts, ISO-week block bootstrap 10,000 draws, seed "
         "20261104; PASS if the 5th percentile > 0. D2 sizing, weighted throughout: return-attribution-weighted log loss "
         "of the base rate minus that of the calibrated probabilities, week-clustered SE; PASS if mean - 1.645 SE > 0. "
         "Gate D = D1 AND D2; exit code IS the verdict. AUC / Mann-Whitney (v3) is a reported diagnostic. A Gate D fail "
         "is a real finding.")

edit_para("Sizing stack: payoff-aware AFML s.10.3 size",
          "Not used, each tested on our data:",
          "Rev 45 (D082): the probability is the live sigmoid calibrator's, fitted in Step 04 with the AFML Ch.4 "
          "return-attribution weights (as the model is trained and Gate D v4.1 scores; the per-fold calibrated_probability "
          "diagnostic stays unweighted). Not used, each tested on our data:")

edit_para("EDGE LEVEL (decides config/edge_universe.json)",
          "Gate D v4: D1 F1 AND D2 log loss (D078)",
          "Gate D v4.1: D1 F1 at calibrated P(win) > b AND D2 weighted log loss (D078)")

appendix = (
    H1("62. Appendix: 2026-11-04 Gate D v4.1 and the Weighted Live Calibrator (Revision 45)")
    + BUL("Owner 2026-11-04. D1 decision point: 'approved'. D2: 'weighted throughout ... approved'. Live calibrator: 'fit "
          "it weighted too'.\n"
          "  - Basis: AFML s.4.6 pp.68-69 (weight labels by absolute return), Snippet 7.4 (cvScore weighted log loss), "
          "s.9.4 pp.134-135 (sample-weighted cross-entropy is 'the right ML performance metric ... for financial "
          "applications'), s.10.3 (size from the classifier's probability; the Platt calibrator is a Quantara addition).\n"
          "  - This reverses my earlier recommendation of an unweighted D2.")
    + BUL("Gate D v4.1 (D078; shared/edges/gate_d_calibration_monotonicity.py, tests/test_gate_d_v4.py):\n"
          "  - The cross-fitted calibrator is weighted, and b is the weighted base rate.\n"
          "  - D1 acts when the calibrated P(win) > b. Its F1 is computed on unweighted counts (s.14.8).\n"
          "  - D2 is the weighted log loss against b.\n"
          "  - Gate D = D1 AND D2.")
    + BUL("D082: Step 04's fit_final_live_sigmoid fits the Platt sigmoid on the stacked CPCV paths with each row's "
          "return-attribution weight (shared AFML weight engine, Task 2A resolution CSV, bar clock); the calibrator JSON "
          "records it. It takes effect at the next Step 04 of any edge; there are no deployable edges now. The per-fold "
          "calibrated_probability diagnostic (D015) stays unweighted. Register D078 (v4.1) and D082; backlog D078 "
          "removed (settled).")
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
