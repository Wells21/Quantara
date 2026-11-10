"""Quantara_Runbook.docx Revision 40 (2026-11-03): MLAM Ch.8 (Testing set overfitting) closed, no change.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev40.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 39 (Revision 38: 3 November 2026;",
                   "Revision date: 3 November 2026 - Revision 40 (Revision 39: 3 November 2026; Revision 38: 3 November 2026;")
after_para("Revision 39 (3 November 2026): MLAM Chapter 7 (portfolio construction) closed with no change. Appendix 56.",
           P("Revision 40 (3 November 2026): MLAM Chapter 8 (testing set overfitting) closed with no change. Appendix 57."))
appendix = (
    H1("57. Appendix: 2026-11-03 MLAM Chapter 8 (Testing Set Overfitting) -- no change")
    + BUL("Line-by-line pass (walkthrough section 4ZD) on top of the AFML Chapter 14 review (section 4N, D054): precision "
          "and recall under a low prior (= the Gate 0 theory check, D075), FWER and familywise miss rate, Mertens Sharpe "
          "variance, the False Strategy theorem, DSR, effective trials by ONC, robust cluster weights (Lopez de Prado & "
          "Lewis 2018), Sidak FWER and Type II errors: all implemented and tested on our data in section 4N.")
    + BUL("No test repeated (owner rule). The 14 unit tests reproduce the book's printed numbers (3.26; z 2.4978; "
          "alpha_K 0.0608; theta 1.9982; beta 0.6913; beta_K 0.0249); the s.8.2 FDR 86.09 % and the alpha 0.0062 / Normal "
          "alpha_K 0.0261 re-checked. Exercise 3: E[max aSR] 1.575, PSR 1.000, DSR 0.828. Exercises 4-5 = the Level 1 "
          "test of section 4N.")
    + BUL("Book code slips recorded (Snippets 8.1-8.3, p.111 simulation count). The D054 gate (DSR > 0.95 and alpha_K "
          "< 0.05, at RECERT) stands. Owner 2026-11-03: 'Approved, close Ch.8'.")
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
