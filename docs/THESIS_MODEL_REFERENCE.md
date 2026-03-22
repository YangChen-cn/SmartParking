# SmartParking Thesis Model Reference

This document summarizes the current project architecture, algorithm definitions, simulation assumptions, metric definitions, and key formulas used in the codebase.

It is intended for thesis writing (methodology + experiment sections) and is aligned with the implementation in:
- `app.py` (runtime system)
- `scripts/simulate_50_slots_compare.py` (baseline vs optimized simulation)
- `templates/index.html` (frontend controls/visualization)

## 1. System Architecture

## 1.1 Runtime architecture
- Frontend dashboard: `templates/index.html` (Bootstrap + Chart.js)
- Backend service: `app.py` (Flask REST API + scheduling/pricing/policy logic)
- Database: `parking.db` (SQLite, slot states, queue, sessions, pricing snapshots, history)
- Edge devices:
- ESP32 firmware: `firmware/src/main.cpp` (slot sensing, trigger/control, env upload)
- K230 module: `k230/main.py` (plate-recognition side integration)

## 1.2 Data flow (runtime)
1. Sensors / ESP32 update slot and environment state.
2. Flask updates active charging state (`update_active_charging_states`).
3. Policy context is built (`build_policy_context`): occupancy, queue, release ratio, flow forecast.
4. Dynamic SOC cap + dynamic pricing are computed.
5. KPI and history APIs provide real-time and aggregate metrics.
6. Frontend polls APIs and renders cards/charts/modal details.

## 1.3 Simulation architecture
- Entry endpoint: `POST /api/simulate_compare` in `app.py`
- Worker script: `scripts/simulate_50_slots_compare.py`
- Outputs:
- JSON report (`sim_50_slots_compare.json`)
- CSV comparison (`sim_50_slots_compare.csv`)

Simulation runs two strategies on the same synthetic request stream:
- Baseline: fixed SOC cap = 100%, fixed price = base price.
- Optimized: dynamic SOC cap + dynamic pricing + price-elasticity demand response.

## 2. Parameter Dictionary

## 2.1 Global runtime constants
- `BASE_PRICE_PER_KWH = 1.20` CNY/kWh
- `SOC_CAP_TIERS = [100, 90, 80, 70, 60]`
- `PEAK_HOURS = {8, 9, 10, 12, 13, 17, 18, 19, 20}`
- `PRICE_SMOOTHING_ALPHA = 0.35`
- `PRICE_STEP_CHANGE_LIMIT = 0.18` CNY/kWh per update
- Operating utilization window: `06:00-24:00` (`OPERATING_START_HOUR = 6`, `OPERATING_END_HOUR = 24`)

## 2.2 Simulation input parameters
- `slots` (int): number of parking/charging slots
- `days` (int): simulation days
- `demand_scale` (float): congestion level / traffic scale
- `seed` (int): RNG seed
- `price_elasticity` (float, symbol `e`): sensitivity of demand to price

Current defaults:
- Frontend default congestion level (traffic scale): `2.0`
- API default `demand_scale`: `2.0`
- Script default `--demand-scale`: `2.0`

## 2.3 Reservation and charging policy parameters (runtime)
- `chargePolicy`: `auto` (charge to dynamic cap) or `timed` (30/60/90/120 min)
- `no_improvement` trigger:
- occupancy = 100%
- release ratio < 0.35
- queue ratio high OR near-future flow high

## 3. Core Model Formulas

## 3.1 Charging curve (piecewise Li-ion approximation)
Charging time is piecewise linear by SOC segment:
- 0% -> 10%: 5 min
- 10% -> 60%: 20 min
- 60% -> 80%: 30 min
- 80% -> 100%: 45 min

Used by:
- `estimate_charge_minutes(start_soc, target_soc)`
- `soc_after_minutes(start_soc, elapsed, cap_soc)`

## 3.2 Dynamic SOC cap model
Load score:
`load_score = 0.50 * occupancy + 0.30 * next_hour_flow + 0.20 * queue_ratio`

Tier mapping:
- `>= 0.85 -> 60`
- `>= 0.70 -> 70`
- `>= 0.55 -> 80`
- `>= 0.35 -> 90`
- else `100`

Special rule:
- If full occupancy and no improvement expected: cap = 60 directly.

Release tightening:
- If `cap > 60` and `release_ratio < 0.20`, tighten one tier.

## 3.3 Dynamic pricing model (runtime and simulation core)
Pressure terms:
- `current_pressure = 0.75 * occupancy + 0.25 * queue_factor`
- `future_pressure = max(current_hour_flow, next_hour_flow)`
- `demand_level = 0.70 * current_pressure + 0.30 * future_pressure`

Adjustment terms:
- `demand_adjust = 0.42 * (demand_level - 0.50)`
- `future_adjust = 0.12 * (future_pressure - 0.45)`
- `queue_adjust = 0.12 * max(0, queue_factor - 0.15)`
- `peak_adjust = 0.10` if peak hour and demand >= 0.35 else `0`
- `release_adjust = 0.05 * max(0, 0.50 - release_ratio)`
- `discount = 0.14 * max(0, 0.50 - current_pressure) + 0.06 * I(demand < 0.35 and not peak)`

Raw price:
`P_raw = P0 * (1 + demand_adjust + future_adjust + queue_adjust + peak_adjust + release_adjust - discount)`

Smoothing:
- `P_smooth = (1 - alpha) * P_prev + alpha * P_raw`
- step clamp: `|P_smooth - P_prev| <= PRICE_STEP_CHANGE_LIMIT`

Peak/off-peak hard bounds (asymmetric tuning):
- Peak hour: `P in [1.05 * P0, 1.72 * P0]`
- Off-peak: `P in [0.62 * P0, 0.96 * P0]`

## 3.4 Price-elasticity demand model (simulation-only layer)
This layer is intentionally simulation-only to emulate user behavioral response.

High-price deferral (instead of drop):
- `price_gap = (P - P0) / P0`
- if `price_gap >= 0`:
`accept_rate = clamp(1 - e * price_gap, 0.55, 1.00)`
- If `demand_draw > accept_rate`, the request is deferred to a later time window (up to bounded rounds), rather than being dropped.

Low-load attraction:
- `idle_room = max(0, 0.68 - occupancy_ratio)`
- `queue_relief = max(0, 0.22 - queue_ratio)`
- `discount_ratio = max(0, (P0 - P) / P0)`
- `extra_prob = clamp(0.90 * idle_room + 0.35 * queue_relief + 2.80 * e * discount_ratio, 0, 0.70)`
- If `attract_draw < extra_prob`, one extra attracted demand is generated.

Note:
- Core SOC + pricing policy is aligned with backend.
- Elasticity deferral/attraction is the simulation-specific extension.

## 4. KPI and Metric Definitions

Let session set be `S`, and wait time of session `i` be `w_i`.

- `Charging Sessions`:
- Number of completed simulated sessions, `|S|`.

- `Avg Duration (min)`:
- `mean(actual_minutes_i)`.

- `Avg Wait (min)`:
- `mean(w_i)`.

- `P95 Wait (min)`:
- Sort waits ascending.
- Index `k = floor(0.95 * (n - 1))`, where `n = |S|`.
- `P95 = wait_sorted[k]`.

- `Queue Hit Rate (%)`:
- Percentage of sessions with positive wait:
- `100 * count(w_i > 0.01) / n`.

- `Utilization (%)`:
- Time-based utilization over operating window only (`06:00-24:00`):
- Numerator: total charging minutes overlapping operating window.
- Denominator: `slots * days * 18h * 60`.
- This same window concept is used for runtime KPI/history and simulation.

- `Demand Served Ratio (%)` (simulation):
- `100 * served_sessions / effective_demand_total`.
- `effective_demand_total = original_requests + attracted_requests`.

- `Price-driven Deferrals` (simulation):
- Number of requests deferred by high-price acceptance filter.

- `Price-driven Attraction` (simulation):
- Number of extra requests added by low-load attraction rule.

- `Avg Price (CNY/kWh)`:
- Mean unit price over sessions.

- `Avg Peak Price (CNY/kWh)`:
- Mean unit price over sessions starting in peak hours.

- `Avg Off-peak Price (CNY/kWh)`:
- Mean unit price over sessions starting in non-peak operating hours.

- `Total Energy (kWh)`:
- Sum of per-session charged energy.

- `Total Revenue (CNY)`:
- Sum of per-session cost.

## 5. Queue Assignment Definition (runtime)

When target slot is unavailable, queue assignment selects the slot with minimum estimated release time:
- If slot is free: wait = 0.
- If charging: use estimated charging end.
- If reserved: use reservation end time.
- Fallback estimates are applied when exact end is unavailable.

This provides:
- Estimated wait shown to users.
- Automatic queue-slot mapping.

## 6. Experiment Protocol Suggestion (for thesis)

For reproducible comparisons:
1. Fix `seed` and run both baseline and optimized on identical generated demand.
2. Evaluate multiple congestion levels (for example 1.0, 1.5, 2.0, 2.5, 3.0).
3. Report at least:
- Sessions
- Avg/P95 wait
- Queue hit rate
- Utilization (06:00-24:00)
- Avg/Peak/Off-peak price
- Energy and revenue
4. Discuss tradeoff regions:
- low/medium congestion: utilization and throughput gains are expected
- extreme congestion: queue reduction may dominate over utilization increase

## 7. Quick Reproduction Commands

Run one simulation:

```bash
python3 scripts/simulate_50_slots_compare.py \
  --slots 50 \
  --days 30 \
  --demand-scale 2.0 \
  --seed 20260321 \
  --price-elasticity 0.28 \
  --output-dir /tmp/sim_report
```

Run via backend API:

```bash
curl -X POST http://localhost:5000/api/simulate_compare \
  -H "Content-Type: application/json" \
  -d '{"slots":50,"days":30,"demand_scale":2.0,"seed":20260321,"price_elasticity":0.28}'
```

---

