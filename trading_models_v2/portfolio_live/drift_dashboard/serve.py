"""
Serves the drift dashboard report over localhost -- not internet-facing.

v2 change (2026-09-24): the server rebuilds the report from live data on
EVERY page load (GET /), not just once at startup. Reloading the browser
tab is enough to see new trades -- there is no separate "re-run the build
script" step anymore. A build failure serves the last good report (if any)
with an error banner injected, rather than crashing the whole dashboard.

Usage:
    python -m portfolio_live.drift_dashboard.serve [--port 8765] [--no-live-refresh]

Then open http://localhost:8765 in a browser. Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import sys
import traceback
import webbrowser
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_V2_ROOT = _THIS_FILE.parents[2]
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))

from portfolio_live.drift_dashboard.build_report import REPORT_DIR, build_report  # noqa: E402

DEFAULT_PORT = 8765

ERROR_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Quantara Drift Dashboard — build error</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#0a0b10;color:#e8eaf2;padding:48px;}}
pre{{background:#161925;border:1px solid #23273a;border-radius:10px;padding:20px;overflow-x:auto;color:#f87171;font-size:13px;}}
h1{{color:#f87171;}}
</style></head>
<body>
<h1>Report rebuild failed</h1>
<p>The dashboard tried to rebuild from live data on this page load and hit an error. Fix the underlying
data issue and reload this tab -- it will try again automatically.</p>
<pre>{traceback}</pre>
</body></html>
"""


def _make_handler(live_refresh: bool):
    class RefreshingHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(REPORT_DIR), **kwargs)

        def do_GET(self):  # noqa: N802
            if live_refresh and self.path in ("/", "/index.html"):
                try:
                    build_report()
                except Exception:
                    body = ERROR_TEMPLATE.format(traceback=traceback.format_exc()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            super().do_GET()

        def log_message(self, fmt, *args):
            print(f"[drift-dashboard] {self.address_string()} - {fmt % args}")

    return RefreshingHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Quantara drift dashboard on localhost.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-live-refresh",
        action="store_true",
        help="Disable rebuild-on-page-load; serve whatever's already in report/ (built once at startup if missing).",
    )
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab.")
    args = parser.parse_args()

    live_refresh = not args.no_live_refresh
    if not live_refresh and not (REPORT_DIR / "index.html").exists():
        print("No existing report found; building once before serving statically.")
        build_report()
    elif live_refresh:
        print("Building initial report...")
        build_report()

    handler = _make_handler(live_refresh)
    url = f"http://localhost:{args.port}"
    mode = "auto-rebuild on every page load" if live_refresh else "static (--no-live-refresh)"
    print(f"Serving Quantara drift dashboard at {url} ({mode}) -- Ctrl+C to stop")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
