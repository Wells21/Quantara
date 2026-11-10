"""Broker cost model (RUNBOOK v5.1, 2026-10-16): fit + validate a prop firm's real trading costs from its own MT5 data.

Built by portfolio_allocator/00c_build_broker_cost_model.py; consumed by Gate C (--execution-spread-model broker) and by
the allocator's re-pricing step 00d. Modules:
  server_tz     -- trade-server clock rule detection (NY_CLOSE / EET / FIXED_n) from the firm's own ticks
  panels        -- READ-ONLY MT5 extraction of per-minute spread/mid panels + symbol specs + deal history
  vol_cache     -- per-minute trailing-1-day realized volatility from the Dukascopy mid (firm-independent)
  spread_model  -- clock alignment, FIXED_POINTS / hour-of-week x vol-state TABLE fit, blocked-week CV, walk-forward
                   level-window error (uncertainty band), recent-2-week level, live-entry calibration
  carry         -- swap spec + commission from real deals
"""
