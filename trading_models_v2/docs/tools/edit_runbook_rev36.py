"""Quantara_Runbook.docx Revision 36 (2026-11-03): MLAM Ch.4 (Optimal clustering) closed -- D077.

Helpers as in edit_runbook_rev33.py.

usage: python edit_runbook_rev36.py IN_DOCX OUT_DOCX
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


ns["replace_once"]("Revision date: 2 November 2026 - Revision 35 (Revision 34: 2 November 2026;",
                   "Revision date: 3 November 2026 - Revision 36 (Revision 35: 2 November 2026; Revision 34: 2 November 2026;")
ns["replace_once"]("clustered with ONC (MLAM Snippets 4.1-4.2) on the normalised",
                   "clustered with ONC (MLAM Snippets 4.1-4.2; Rev 36 / D077: the best silhouette-quality partition over 20 "
                   "restarts x 50 initialisations, ties to the lowest seed) on the normalised")
after_para("Revision 35 (2 November 2026): MLAM Chapter 3 (distance metrics) closed with no change. Appendix 52.",
           P("Revision 36 (3 November 2026): MLAM Chapter 4 (optimal clustering) closed. Step 01 feature ONC keeps the "
             "best-quality partition over 20 restarts of 50 initialisations (D077); backtest-trial ONC unchanged (D076, "
             "no p.55 reduction). Section 6A Step 01. Appendix 53."))
appendix = (
    H1("53. Appendix: 2026-11-03 MLAM Chapter 4 (Optimal Clustering) -- D077")
    + BUL("Audit and nine pre-registered tests (walkthrough sections 4Z-4Z.4; logs/mlam_ch4_tests_20261102). T1: the book's "
          "Monte Carlo (Snippet 4.3) reproduced, median E[K]/K = 1.00. T2/T7: backtest-trial ONC (D076, unreduced) gives "
          "identical E[K], DSR and FWER on every seed at 10 and 50 initialisations -- kept as is. T4/T6: the p.55 PCA "
          "reduction of the trial matrix is seed-unstable at 10 initialisations and moves E[K] to 2 at 50 -- not adopted.")
    + BUL("Step 01 feature ONC: at 10 initialisations the cluster count swung 2-26 across seeds (T3); at 50 the count is 2 "
          "but the kept feature set still depends on the seed on 3/6 edges (T8: JPN volatilitycarry 126/30/28, XAUUSD "
          "volatilitycarry 111/28/155). Cause: the search does not reach ONC's own objective (max quality q). T9: the "
          "best-q partition over 20 restarts gives the same kept feature set in every block on all 6 edges.")
    + BUL("Owner 2026-11-02/03: 'Approved 1 and 2', 'Approved A', 'Approved, close Ch.4'. Step 01 = onc_best_of_restarts "
          "(shared/features/clustered_feature_importance.py, STEP01_ONC_RESTARTS 20, STEP01_ONC_N_INIT 50, CFI v2). Stored "
          "Step 01 outputs change at the RECERT (volatilitycarry edges keep 123 / 32 features at the max-q partition). "
          "Record: config/mldp_deviation_register.json D077; backlog D077; standing rule ONC_SEARCH_CONVERGENCE_D077.")
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
