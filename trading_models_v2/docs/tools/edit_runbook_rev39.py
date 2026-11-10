"""Quantara_Runbook.docx Revision 39 (2026-11-03): MLAM Ch.7 (Portfolio construction) closed, no change.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev39.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 38 (Revision 37: 3 November 2026;",
                   "Revision date: 3 November 2026 - Revision 39 (Revision 38: 3 November 2026; Revision 37: 3 November 2026;")
after_para("Revision 38 (3 November 2026): MLAM Chapter 6 (feature importance analysis) closed with no change. Appendix 55.",
           P("Revision 39 (3 November 2026): MLAM Chapter 7 (portfolio construction) closed with no change. Appendix 56."))
appendix = (
    H1("56. Appendix: 2026-11-03 MLAM Chapter 7 (Portfolio Construction) -- no change")
    + BUL("Line-by-line pass (walkthrough section 4ZC) on top of the AFML Chapter 16 review (section 4P, D010, D057-D060): "
          "optimiser formulas, condition number, Markowitz's curse and the Monte Carlo rule (D060) already in place.")
    + BUL("T1a: the condition numbers of Snippets 7.1-7.2 and section 7.5 reproduced exactly (3, 4, 7, 251, 3). The "
          "synthetic Figure 7.2-7.3 replication and Exercises 1-5 simulations were stopped on the owner's instruction "
          "(they test the book, not Quantara); the exercises are answered from the text and our results.")
    + BUL("T3 on the 4 deployable edges (daily mark-to-market, condition number 1.28): no NCO variant (min-variance, "
          "abs(corr) clustering, IVP or long-only CLA inside) is more stable than HRP-current at nObs 260 or T. HRP stays "
          "(D060).")
    + BUL("Book errata recorded: p.101 'fifty securities' is 500 in Snippet 7.7; p.102 shrinkage 'helps reduce' "
          "contradicts Figure 7.2 (+11.8 %); Figure 7.2 vs Ch.2 Figure 2.5 raw RMSE differ; Snippet 7.3 / Ch.2 p.36 code "
          "slips. Owner 2026-11-03: 'Approved, close Ch.7 without the extra'.")
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
