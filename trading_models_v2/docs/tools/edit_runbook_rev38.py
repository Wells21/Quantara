"""Quantara_Runbook.docx Revision 38 (2026-11-03): MLAM Ch.6 (Feature importance analysis) closed, no change.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev38.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 37 (Revision 36: 3 November 2026;",
                   "Revision date: 3 November 2026 - Revision 38 (Revision 37: 3 November 2026; Revision 36: 3 November 2026;")
after_para("Revision 37 (3 November 2026): MLAM Chapter 5 (financial labels) closed with no change. Appendix 54.",
           P("Revision 38 (3 November 2026): MLAM Chapter 6 (feature importance analysis) closed with no change. Appendix 55."))
appendix = (
    H1("55. Appendix: 2026-11-03 MLAM Chapter 6 (Feature Importance Analysis) -- no change")
    + BUL("Audit (walkthrough section 4ZB): Step 01 already implements Ch.6 (VI clusters, best-of-20 ONC (D077), clustered "
          "MDA + MDI, log-loss score, purged 10-fold, rule B mean - 1.645 se > 0); Exercises 1-2 answered.")
    + BUL("T1: the book's synthetic data (Snippet 6.1) through the production Step 01: VI + ONC recovers the 6-group truth "
          "exactly; rule B drops noise but keeps only 2 of 5 signal clusters. T1m (post hoc): with importance-tree "
          "min_weight_fraction_leaf 0 (Snippet 6.8) all 5 are kept but a near-zero noise cluster also passes.")
    + BUL("T4 (post hoc, owner-approved, rule pre-registered): Step 01 on the 6 edges, mwfl 0.05 (D041) vs 0: mwfl 0 keeps "
          "fewer clusters on 5 of 6 (none on 3) and has worse out-of-sample log loss on all 6. D041 stands; Step 01 is "
          "conservative on clean non-overlapping data, recorded as a known limitation.")
    + BUL("T2: probability-weighted accuracy instead of log loss gives the same kept set on 4 of 6 edges; log loss stays "
          "(book p.82). T3/T3r: residual features (s.6.5.2) give kept sets that depend on cluster order on all 6 edges; "
          "D042 (no residualisation) stands.")
    + BUL("Section 6.7 p.91 (importance can change with the labelling method) is a Gate 0 thesis-design note. Owner "
          "2026-11-03: 'I approve' (close Ch.6).")
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
