"""Quantara_Runbook.docx Revision 27 (2026-10-31): AFML Ch.18 walkthrough (entropy features).

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev26.py.

usage: python edit_runbook_rev27.py IN_DOCX OUT_DOCX
"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
head = (HERE / "edit_runbook_rev17.py").read_text(encoding="utf-8")
head = head[: head.index('replace_once("Revision date: 24 October 2026 - Revision 16')]
ns: dict = {"__name__": "rev17_head"}
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
sys.argv = [sys.argv[0], str(src), str(dst)]
exec(compile(head, "edit_runbook_rev17.py(head)", "exec"), ns)
P, H1, H2, BUL, CODE, WARN = ns["P"], ns["H1"], ns["H2"], ns["BUL"], ns["CODE"], ns["WARN"]
DOT = ns["DOT"]

ns["replace_once"]("Revision date: 30 October 2026 - Revision 26 (Revision 25: 30 October 2026;",
                   "Revision date: 31 October 2026 - Revision 27 (Revision 26: 30 October 2026; Revision 25: 30 October 2026;")
ns["insert_after_para"](
    "Revision 26 (30 October 2026): AFML Chapter 17",
    P("Revision 27 (31 October 2026): AFML Chapter 18 (entropy features). Adds Section 6A Step 10N (entropy features: "
      "Snippets 18.3-18.4 as printed in code, three encodings with frozen per-asset boundaries; D066-D069; Step 07 "
      "effective number of bets, D070) and Appendix 44 (the Chapter 18 record)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10N - Entropy features (Rev 27, AFML Ch.18 pp.263-279)")
    + P("shared/features/afml_ch18_entropy.entropy_row computes 3 columns at EVERY bar from the 101 closes ending at that "
        "bar (100 log returns, lag 0), identically in research (feature_expansion.py) and live "
        "(native_one_row_feature_engine). Phase C.2 certifies live against the research batch and the research rule "
        "(entropy_research_rule_parity) at every step.")
    + BUL("Estimator: Kontoyiannis, Snippet 18.4 with its default expanding window; match length = 1 + the longest match "
          "(Snippet 18.3; the old port was one short and failed the book's p.269 example) (D066). An odd-length message "
          "loses its first letter (p.269).")
    + BUL("Entropy_Kontoyiannis_Binary_100 (sign, zeros removed), _Quantile10_100 (10 letters), _Sigma_100 (step = std of "
          "all research bar returns, ex. 18.1(c)). Entropy_Kontoyiannis_100 (sign of Close_FFD.diff()) is removed; not "
          "encoding the FFD series of p.270 is a registered deviation (D068).")
    + BUL("Quantile boundaries and sigma are frozen per asset on the research history, like the FFD d: "
          "assets/{A}/bars/entropy/entropy_encoding_manifest.json (D069). Re-run the builder only when the research bars "
          "change, then re-export every edge of that asset.")
    + BUL("All edges carry the same 188-entry feature_schema; Step 01 decides which columns enter each model.")
    + BUL("Allocator Step 07 reports pc_effective_number_of_bets and pc_meucci_concentration (s.18.8.3) in the portfolio "
          "summary; report only, in no score or gate (D070).")
    + WARN("Phase C before the RECERT retrain",
           "Until the RECERT retrain, models that select the removed Entropy_Kontoyiannis_100 (HK dhc, JPN rs, XAU rs, "
           "XAU vc) cannot be compiled into Phase C.1; retrain them before any Phase C regeneration.")
    + CODE(["..\\venv312\\Scripts\\python.exe shared\\features\\build_asset_entropy_encoding.py ASSET",
            "..\\venv39\\Scripts\\python.exe -m pytest tests/test_afml_ch18_entropy.py tests/test_afml_ch18_portfolio_concentration.py"]))
appendix = (
    H1("44. Appendix: 2026-10-31 AFML Chapter 18 Walkthrough (Revision 27)")
    + BUL("Before: one feature, Entropy_Kontoyiannis_100, in all 6 edges (a model input of HK dhc, JPN rs, XAU rs); "
          "research = live (no skew), MDA within about one std of 0. The match length was one short (295 of 300 random "
          "cases differed from Snippet 18.3; the book's p.269 example gave 1.34 / 1.34 instead of 0.97 / 0.84), the "
          "match window len/4 was not from the book, and binary encoding of tick bars (|r| p90/p10 = 15-16) discards size.")
    + BUL("After: code = verbatim Snippets 18.3-18.4 on > 10,000 cases; p.269 example 0.968 / 0.843 (printed truncated as "
          "0.96 / 0.84); quantile letters 0.100 each in sample on all 10 assets; 0.8 ms per bar for the three columns; "
          "Phase C.2 PASS on venv39 (GBPJPY, JPN) with the three columns compared every step.")
    + BUL("Book errata: p.266 match-length formula one short; Snippet 18.4 redundancy uses log2(len) (p.264: log2 of the "
          "alphabet); p.268 example (n = 198, k = 58, N = 256) cannot run with Snippet 18.4; Figure 18.1 does not "
          "reproduce (book 1.43 / 1.21 / 0.99 / 0.46, code 2.29 / 2.03 / 1.75 / 0.85) and compares bits with nats; "
          "N_q = k for every q != 1; exercise 18.5(b)-(c) shares sum to 9; Easley et al. 2012a pages 1457-1493.")
    + BUL("Not applied: entropy-implied volatility (s.18.6), maximum entropy generation (s.18.8.2, no procedure), "
          "order-flow-imbalance entropy (s.18.8.4, needs classified traded volume; we hold quote ticks). Exercises "
          "18.1-18.2 need E-mini dollar bars (not held); 18.3-18.5 were run.")
    + BUL("pmf1 counts every word (Snippet 18.1 drops the last); registered deviation D067, unused.")
)
x = ns["x"]
i = x.rfind("<w:sectPr")
x = x[:i] + appendix + x[i:]
zin = ns["zin"]
tmp = Path(tempfile.mkdtemp()) / "out.docx"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = x.encode("utf-8") if item.filename == "word/document.xml" else zin.read(item.filename)
        zout.writestr(item, data)
shutil.move(str(tmp), dst)
print(f"written {dst}")
