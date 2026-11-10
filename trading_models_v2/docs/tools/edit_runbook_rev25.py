"""Quantara_Runbook.docx Revision 25 (2026-10-30): AFML Ch.16 walkthrough (machine learning asset allocation, with MLAM Ch.7).

Helpers are the Revision 17 editor's (docs/tools/edit_runbook_rev17.py head), as in edit_runbook_rev18.py - rev24.py.

usage: python edit_runbook_rev25.py IN_DOCX OUT_DOCX
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

ns["replace_once"]("Revision date: 29 October 2026 - Revision 24 (Revision 23: 27 October 2026;",
                   "Revision date: 30 October 2026 - Revision 25 (Revision 24: 29 October 2026; Revision 23: 27 October 2026;")
ns["insert_after_para"](
    "Revision 24 (29 October 2026): GEOMETRY RULE D050-A",
    P("Revision 25 (30 October 2026): AFML Chapter 16 (machine learning asset allocation, with MLAM Ch.2 and Ch.7). Adds "
      "Section 6A Step 10L (allocator streams, denoising and HRP; D010, D057-D060) and Appendix 42 (the Chapter 16 record)."))
ns["insert_before_para"](
    f"6A {DOT} STEP 12B - GATE D: meta-model discrimination",
    H2(f"6A {DOT} STEP 10L - Allocator streams, denoising and HRP (Rev 25, AFML Ch.16 pp.221-245, MLAM Ch.2 pp.24-37, Ch.7)")
    + P("All allocator steps run on venv39 (07's clean_corr fails under pandas 3 copy-on-write; no version pin, owner).")
    + BUL("06 (D059, AFML exercise 16.1(a) p.244): returns are aligned to the average bet frequency (s.14.3 bets): the largest "
          "of daily / weekly / monthly not longer than the average interval between bets, floor daily. 06 applies the rule on "
          "every run and stops for an owner decision if it selects other than daily. Daily streams are booked by "
          "mark-to-market: an open trade is marked at each UTC day's last research mid bar close, the exit day books the move "
          "to exit_price plus the cost residual; each trade's daily R sums to executed_r (fail closed). Check the 06 manifest: "
          "date_aggregation mark_to_market_daily, bet_frequency_alignment daily.")
    + BUL("07 (D010, MLAM Snippets 2.4-2.5 pp.27-29): Marcenko-Pastur fit with the book's var = 1 fallback; nFacts = eigenvalues "
          "above lambda+ with no floor; constant residual eigenvalue (nFacts = 0 gives the identity, so HRP = IVP). Detone "
          "(s.2.6 p.30) only if a market component exists: first eigenvalue > lambda+ AND first-eigenvector loadings of one "
          "sign in >= 95 % of 1,000 iid row-bootstrap resamples (seed 0); a detoned matrix is used for clustering only "
          "(singular, p.32). Rule id mlam_ch2_book_v2; every test is written to hrp_denoise_diagnostics_*.json.")
    + BUL("07 (D058, Snippet 16.4): the tree is built on d~ = the Euclidean distance between the columns of D; 07b / 07c "
          "validate the single-linkage portfolio that live trades.")
    + BUL("Allocation method (D060): HRP kept, max-Sharpe methods rejected for now (MLAM s.7.8). Re-run T1-T4 "
          "(shared/research/ch16_allocation_tests.py) on the deployable edges at every edge_universe change and at the "
          "RECERT; a method replaces HRP only under the walkthrough s.4P.1 decision rule and with owner approval.")
    + BUL("D057 (diagnostic, not a gate): threshold-selection PBO and the threshold-inclusive DSR "
          "(shared/research/allocator_threshold_pbo.py) are reported at every RECERT beside the D054 gate, whose trial "
          "count stays Gate C only.")
    + CODE(["..\\venv39\\Scripts\\python.exe -m pytest tests/test_afml_ch16_hrp.py",
            "..\\venv39\\Scripts\\python.exe -m shared.research.ch16_allocation_tests --out artifacts/ch16/ch16_allocation_tests.json --workers 4",
            "..\\venv39\\Scripts\\python.exe -m shared.research.allocator_threshold_pbo --out artifacts/ch16/d057_threshold_pbo.json"]))
appendix = (
    H1("42. Appendix: 2026-10-30 AFML Chapter 16 Walkthrough (Revision 25)")
    + BUL("Book check: the tools reproduce Table 16.1 (CLA / HRP / IVP, p.233) and the condition number 150.9324 (p.231); "
          "on the book's data d vs d~ moves HRP weights by up to 7.83 pp, on ours not at all.")
    + BUL("T1-T4 (10,000 Monte Carlo iterations, exit-day and mark-to-market daily streams): no method beats the current HRP; "
          "IVP ties it; CLA, NCO and the book's HRP are not lower; max-Sharpe methods have 255x to 79,412x its out-of-sample variance; in the "
          "spectral stress test Markowitz / CLA / NCO move most, HRP / IVP least.")
    + BUL("Current 4 edges under the new 07: fitted variance 0.961, lambda+ 1.062, nFacts 1, bootstrap same-sign share 0.002 "
          "(no detone). HRP weights GBPJPY / JPN / HK / XAU: exit-day 0.197 / 0.086 / 0.208 / 0.509, mark-to-market "
          "0.218 / 0.079 / 0.204 / 0.499; condition numbers 1.24 / 1.28.")
    + BUL("Bet frequency: 107-111 bets / year averaged over edges, one bet every 3.3-3.4 days -> daily.")
    + BUL("D057 threshold PBO: XAU 0.230, GBPJPY 0.510, HK 0.567, JPN 0.815 (Prob[OOS <= 0] 0.005 / 0.036 / 0.106 / 0.434).")
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
