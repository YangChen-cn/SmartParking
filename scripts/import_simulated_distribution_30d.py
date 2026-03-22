#!/usr/bin/env python3
"""Import simulated charging time-slot distribution into parking.db.

This script generates campus-like charging requests for the recent N days,
transforms them into charging_sessions rows, and writes them into SQLite.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import BASE_PRICE_PER_KWH, BATTERY_CAPACITY_KWH, DATETIME_FMT, get_conn, init_db  # noqa: E402
from scripts.simulate_50_slots_compare import (  # noqa: E402
    clamp,
    clamp_to_operating_arrival,
    estimate_charge_minutes,
    generate_campus_requests,
    is_peak_hour,
    soc_after_minutes,
)


def to_session_row(req, index: int, total_slots: int, peak_mult: float, offpeak_mult: float):
    start_time = clamp_to_operating_arrival(req.arrival)

    if req.mode == "manual":
        planned_minutes = float(req.manual_minutes or 30)
    else:
        # Auto mode uses full charge estimation to 100% for data generation.
        planned_minutes = estimate_charge_minutes(req.initial_soc, 100.0)

    planned_minutes = max(6.0, planned_minutes)
    actual_minutes = clamp(planned_minutes * req.duration_factor, 6.0, 150.0)
    end_time = start_time + dt.timedelta(minutes=actual_minutes)

    final_soc = soc_after_minutes(req.initial_soc, actual_minutes, 100.0)
    delta_soc = max(0.0, final_soc - req.initial_soc)
    energy_kwh = round(BATTERY_CAPACITY_KWH * delta_soc / 100.0, 3)

    unit_price = BASE_PRICE_PER_KWH * (peak_mult if is_peak_hour(start_time) else offpeak_mult)
    unit_price = round(unit_price, 2)
    total_cost = round(energy_kwh * unit_price, 2)

    slot_id = (index % max(1, total_slots)) + 1
    day_tag = start_time.strftime("%Y%m%d")
    license_plate = f"SIM{day_tag}{index:05d}"

    return (
        slot_id,
        license_plate,
        start_time.strftime(DATETIME_FMT),
        end_time.strftime(DATETIME_FMT),
        round(req.initial_soc, 2),
        round(final_soc, 2),
        100.0,
        100,
        req.mode,
        req.manual_minutes if req.mode == "manual" else None,
        round(planned_minutes, 2),
        round(actual_minutes, 2),
        0.0,
        energy_kwh,
        unit_price,
        total_cost,
        "sim_import",
    )


def main():
    parser = argparse.ArgumentParser(description="Import simulated charging distribution into charging_sessions")
    parser.add_argument("--days", type=int, default=30, help="Recent days to generate (default: 30)")
    parser.add_argument("--slots", type=int, default=14, help="Slot count used for simulation scale (default: 14)")
    parser.add_argument("--seed", type=int, default=20260321, help="Random seed for reproducibility")
    parser.add_argument("--demand-scale", type=float, default=2.0, help="Congestion level in [1,4]")
    parser.add_argument("--peak-mult", type=float, default=1.18, help="Peak-hour price multiplier")
    parser.add_argument("--offpeak-mult", type=float, default=0.88, help="Off-peak price multiplier")
    parser.add_argument("--append", action="store_true", help="Append data instead of replacing generated window")
    parser.add_argument("--dry-run", action="store_true", help="Generate and print summary without writing DB")
    args = parser.parse_args()

    if args.days < 1:
        raise ValueError("days must be >= 1")

    now = dt.datetime.now().replace(second=0, microsecond=0)
    rng = random.Random(args.seed)

    init_db()

    requests = generate_campus_requests(
        days=args.days,
        seed=args.seed,
        now=now,
        demand_scale=args.demand_scale,
        slots=args.slots,
    )

    rows = [
        to_session_row(
            req=req,
            index=i,
            total_slots=args.slots,
            peak_mult=args.peak_mult,
            offpeak_mult=args.offpeak_mult,
        )
        for i, req in enumerate(requests, start=1)
    ]

    # Shuffle only slot assignment order for more realistic slot spread while
    # keeping start-time distribution unchanged.
    if rows:
        rows = rows[:]
        rng.shuffle(rows)

    hour_dist = Counter(int(row[2][11:13]) for row in rows)
    start_window = (now - dt.timedelta(days=args.days)).strftime(DATETIME_FMT)

    print(f"Prepared rows: {len(rows)}")
    print(f"Time window start: {start_window}")
    print("Hour distribution:")
    for hour in range(24):
        if hour_dist.get(hour, 0) > 0:
            print(f"  {hour:02d}:00 -> {hour_dist[hour]}")

    if args.dry_run:
        print("Dry-run enabled. No database changes were made.")
        return

    conn = get_conn()
    c = conn.cursor()

    if not args.append:
        c.execute("DELETE FROM charging_sessions WHERE start_time >= ?", (start_window,))

    c.executemany(
        """
        INSERT INTO charging_sessions
        (slot_id, license_plate, start_time, end_time, initial_soc, final_soc, target_soc,
         dynamic_cap, mode, manual_minutes, planned_minutes, actual_minutes, wait_minutes,
         energy_kwh, unit_price, total_cost, end_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    conn.commit()
    conn.close()

    print(f"Inserted rows: {len(rows)}")
    if args.append:
        print("Mode: append")
    else:
        print(f"Mode: replace from {start_window}")


if __name__ == "__main__":
    main()
