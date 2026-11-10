# Canonical Gate A / Gate C search grid -- D093 (owner 2026-11-10). Source this file in every Gate A queue script:
#   . shared/research/gate_a_grid.sh
#   python shared/research/v3_horizon_lab.py ... $GRID_ARGS --cost-bps <broker mean> --hybrid-fast-grid --save
# 5 event-vol lookbacks x 1 EWM span x 8 vertical barriers x 3 PT:SL = 120 cells; every cell is a paid trial (D050-A).
# Was 5 x 3 (EWM 100,150,200; D074) x 9 (1..168 h) x 3 = 405. EWM 100/150/200 results correlate 0.98-0.99 and never
# formed their own DSR cluster; 168 h was reached by 0.3 % of trades and tracks 96 h (corr 0.996, |diff| 0.003 R).
GRID_LOOKBACKS=1,2,4,6,8
GRID_EWM_SPANS=150
GRID_VERTICALS=1,2,4,8,16,24,48,96
GRID_BARRIERS=1:1,1.5:1,2:1
GRID_N_CELLS=120
GRID_ARGS="--event-vol-lookbacks $GRID_LOOKBACKS --event-vol-ewm-spans $GRID_EWM_SPANS --vertical-horizons $GRID_VERTICALS --barriers $GRID_BARRIERS"
