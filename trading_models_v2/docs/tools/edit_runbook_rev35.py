"""Quantara_Runbook.docx Revision 35 (2026-11-02): MLAM Ch.3 (Distance metrics) closed, no change.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev35.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 2 November 2026 - Revision 34 (Revision 33: 2 November 2026;",
                   "Revision date: 2 November 2026 - Revision 35 (Revision 34: 2 November 2026; Revision 33: 2 November 2026;")
after_para("Revision 34 (2 November 2026): MLAM Chapter 2.",
           P("Revision 35 (2 November 2026): MLAM Chapter 3 (distance metrics) closed with no change. Appendix 52."))
appendix = (
    H1("52. Appendix: 2026-11-02 MLAM Chapter 3 (Distance Metrics) -- no change")
    + BUL("Audit (walkthrough section 4Y): Step 01's num_bins / var_info are Snippet 3.3; the book's Snippet 3.4 results "
          "(NMI 0.0068 / 0.8907 / 0.6439, correlation 0.0015 / 0.9999 / -0.008) reproduce exactly with our code; HRP and "
          "trial clustering use sqrt((1 - rho)/2), the book's long-only case (edge streams carry their side, weights are "
          "positive); Step 01 clusters features on normalised variation of information (p.50); classification scored "
          "on cross-entropy / log loss (p.41). Partition distance (s.3.10) not used. Owner 2026-11-02: 'Close Ch.3'.")
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
