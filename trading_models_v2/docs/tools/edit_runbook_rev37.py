"""Quantara_Runbook.docx Revision 37 (2026-11-03): MLAM Ch.5 (Financial labels) closed, no change.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev37.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 3 November 2026 - Revision 36 (Revision 35: 2 November 2026;",
                   "Revision date: 3 November 2026 - Revision 37 (Revision 36: 3 November 2026; Revision 35: 2 November 2026;")
after_para("Revision 36 (3 November 2026): MLAM Chapter 4 (optimal clustering) closed.",
           P("Revision 37 (3 November 2026): MLAM Chapter 5 (financial labels) closed with no change. Appendix 54."))
appendix = (
    H1("54. Appendix: 2026-11-03 MLAM Chapter 5 (Financial Labels) -- no change")
    + BUL("Audit (walkthrough section 4ZA): fixed-horizon labels not used (p.66); triple barrier with sign verticals and "
          "the searched, paid-for horizon (D050-A); meta-labels int(label_r > 0); bet size by expected Sharpe kept on the "
          "payoff-aware probability (D007; the book's formula assumes a symmetric payoff, ours is 2:1).")
    + BUL("T1: trend scanning (Snippets 5.1-5.3) replicated (median agreement with the true trend 0.89); available as a "
          "primary label for new theses at Gate 0, not a meta-label replacement.")
    + BUL("T2: labels are hour-of-day dependent on both XAUUSD edges (exit reason vs 4-hour entry block, Cramer's V "
          ".13-.14); not on the other four. Recorded as a known label property.")
    + BUL("T3: intraday-seasonal barriers (Event_Vol x predicted-variance ratio of the H hours from entry, causal 60-day "
          "hourly profile), full 405-cell Gates A-C on both XAUUSD edges: hour dependence not removed (V .146 / .107), "
          "rangestructure velocity worse, volatilitycarry better only through a different cell. Pre-registered rule not "
          "met; barriers unchanged. The 810 cells are logged trials (D050-A).")
    + BUL("Ensemble bet sizing (pp.70-71) deferred to the Ch.10 out-of-sample sizing benchmark. Owner 2026-11-03: "
          "'Approved, close Ch.5'.")
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
