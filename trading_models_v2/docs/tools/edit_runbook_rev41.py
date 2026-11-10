"""Quantara_Runbook.docx Revision 41 (2026-11-03): MLAM Appendix A (+ App. B) closed, no change; walkthrough complete.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev41.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 40 (Revision 39: 3 November 2026;",
                   "Revision date: 3 November 2026 - Revision 41 (Revision 40: 3 November 2026; Revision 39: 3 November 2026;")
after_para("Revision 40 (3 November 2026): MLAM Chapter 8 (testing set overfitting) closed with no change. Appendix 57.",
           P("Revision 41 (3 November 2026): MLAM Appendix A (testing on synthetic data) and Appendix B closed with no "
             "change; the AFML and MLAM walkthrough is complete. Appendix 58."))
appendix = (
    H1("58. Appendix: 2026-11-03 MLAM Appendix A (Testing on Synthetic Data) and Appendix B -- no change")
    + BUL("Appendix A (Figure A.1) mapped to Quantara (walkthrough section 4ZE): combinatorial CV = CPCV paths (D052, at "
          "the retrain); purged k-fold (Steps 01-03); permutation-type tests (alpha-independence lab, Gate 0 random-entry "
          "baseline D075); block bootstrap (D060 allocation Monte Carlo); sequential bootstrap (Step 01, training); "
          "parametric Monte Carlo (D060, AFML Ch.13 D053, allocator Step 11). VAE / SOM / GAN not used: the book prefers "
          "parametric methods unless the sample is representative enough (about 4 years of price-only data).")
    + BUL("Appendix B: the False Strategy theorem's expected maximum (eq. 3-4) holds for K >> 1. At E[K] = 2 (every edge's "
          "Level 1 DSR) it is 7.9 % below the exact value; recomputed DSRs change by at most 0.04 and no D054 verdict "
          "changes. The book formula stays; caveat recorded.")
    + BUL("The AFML (Chapters 1-22) and MLAM (Chapters 1-8, Appendices A-B) walkthrough is complete. Next: RECERT and the "
          "retrain. Owner 2026-11-03: 'Approved, close Appendix A'.")
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
