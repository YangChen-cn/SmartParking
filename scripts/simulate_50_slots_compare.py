#!/usr/bin/env python3
"""
Automatic simulation for campus parking/charging slots.

Compares:
1) Baseline (unoptimized): fixed SOC cap=100, fixed price=1.20 CNY/kWh
2) Optimized: dynamic SOC cap + dynamic pricing (same simplified policy as current app)

Outputs:
- Console comparison table
- JSON report: static/sim_50_slots_compare.json
- CSV report:  static/sim_50_slots_compare.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import heapq
import json
import math
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from statistics import mean


BASE_PRICE_PER_KWH = 1.20
BATTERY_CAPACITY_KWH = 60.0
MANUAL_MINUTES_OPTIONS = (30, 60, 90, 120)
SOC_CAP_TIERS = (100, 90, 80, 70, 60)
WEEKDAY_PEAK_HOURS = {8, 10, 12, 17, 18}
WEEKEND_PEAK_HOURS = {10, 17, 18}
OPERATING_START_HOUR = 6
OPERATING_END_HOUR = 24
CAMPUS_REFERENCE_SLOTS = 14
CAMPUS_WEEKDAY_BASE_ARRIVALS = 80
CAMPUS_WEEKEND_BASE_ARRIVALS = 40

# Piecewise charging curve (same as app.py)
CHARGE_SEGMENTS = (
    (0.0, 10.0, 5.0),
    (10.0, 60.0, 20.0),
    (60.0, 80.0, 30.0),
    (80.0, 100.0, 45.0),
)


@dataclass(frozen=True)
class ChargeRequest:
    arrival: dt.datetime
    initial_soc: float
    mode: str  # "auto" or "manual"
    manual_minutes: int | None
    duration_factor: float
    demand_draw: float
    attract_draw: float


@dataclass
class ChargeSession:
    arrival: dt.datetime
    start: dt.datetime
    end: dt.datetime
    wait_minutes: float
    planned_minutes: float
    actual_minutes: float
    initial_soc: float
    final_soc: float
    cap_soc: int
    unit_price: float
    energy_kwh: float
    total_cost: float


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def operating_window_minutes(start_dt: dt.datetime, end_dt: dt.datetime) -> float:
    if end_dt <= start_dt:
        return 0.0

    total = 0.0
    day = start_dt.date()
    last_day = end_dt.date()
    while day <= last_day:
        win_start = dt.datetime.combine(day, dt.time(hour=OPERATING_START_HOUR, minute=0))
        if OPERATING_END_HOUR >= 24:
            win_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(hour=0, minute=0))
        else:
            win_end = dt.datetime.combine(day, dt.time(hour=OPERATING_END_HOUR, minute=0))

        seg_start = max(start_dt, win_start)
        seg_end = min(end_dt, win_end)
        if seg_end > seg_start:
            total += (seg_end - seg_start).total_seconds() / 60.0
        day += dt.timedelta(days=1)
    return total


def estimate_charge_minutes(start_soc: float, target_soc: float) -> float:
    start_soc = clamp(float(start_soc), 0.0, 100.0)
    target_soc = clamp(float(target_soc), 0.0, 100.0)
    if target_soc <= start_soc:
        return 0.0
    total = 0.0
    for seg_start, seg_end, seg_minutes in CHARGE_SEGMENTS:
        overlap_start = max(start_soc, seg_start)
        overlap_end = min(target_soc, seg_end)
        if overlap_end <= overlap_start:
            continue
        total += ((overlap_end - overlap_start) / (seg_end - seg_start)) * seg_minutes
    return total


def soc_after_minutes(start_soc: float, elapsed_minutes: float, cap_soc: float) -> float:
    start_soc = clamp(float(start_soc), 0.0, 100.0)
    cap_soc = clamp(float(cap_soc), 0.0, 100.0)
    remain = max(0.0, float(elapsed_minutes))
    if start_soc >= cap_soc:
        return round(cap_soc, 2)
    soc = start_soc
    for seg_start, seg_end, seg_minutes in CHARGE_SEGMENTS:
        if soc >= cap_soc:
            break
        part_start = max(seg_start, soc)
        part_end = min(seg_end, cap_soc)
        if part_end <= part_start:
            continue
        part_soc = part_end - part_start
        part_minutes = (part_soc / (seg_end - seg_start)) * seg_minutes
        if remain >= part_minutes:
            soc = part_end
            remain -= part_minutes
        else:
            rate = (seg_end - seg_start) / seg_minutes
            soc = part_start + remain * rate
            break
    return round(min(soc, cap_soc), 2)


def weighted_hour(rng: random.Random, weekday: bool) -> int:
    if weekday:
        hours = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
        # Explicit weekday peaks: 8/10/12/17/18.
        weights = [0.02, 0.17, 0.03, 0.17, 0.03, 0.16, 0.05, 0.04, 0.03, 0.04, 0.12, 0.10, 0.02, 0.01, 0.01]
    else:
        hours = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
        # Explicit weekend peaks: 10/17/18.
        weights = [0.03, 0.04, 0.24, 0.05, 0.06, 0.05, 0.05, 0.04, 0.05, 0.18, 0.15, 0.04, 0.02]
    return rng.choices(hours, weights=weights, k=1)[0]


def demand_multiplier_from_level(level: float) -> float:
    # Stronger congestion mapping for clearer scenario separation:
    # 1,2,3,4 -> 0.75,1.00,1.35,1.80 with linear interpolation.
    lv = clamp(float(level), 1.0, 4.0)
    anchors = ((1.0, 0.75), (2.0, 1.00), (3.0, 1.35), (4.0, 1.80))
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= lv <= x1:
            ratio = (lv - x0) / max(1e-9, (x1 - x0))
            return y0 + ratio * (y1 - y0)
    return 1.80


def is_peak_hour_by_parts(hour: int, is_weekday: bool) -> bool:
    return hour in (WEEKDAY_PEAK_HOURS if is_weekday else WEEKEND_PEAK_HOURS)


def is_peak_hour(dt_obj: dt.datetime) -> bool:
    return is_peak_hour_by_parts(dt_obj.hour, dt_obj.weekday() < 5)


def is_shoulder_hour(hour: int, is_weekday: bool) -> bool:
    if is_weekday:
        return hour in {9, 11, 13, 14, 15, 16, 19, 20}
    return hour in {9, 11, 12, 13, 14, 15, 16, 19, 20}


def clamp_to_operating_arrival(ts: dt.datetime) -> dt.datetime:
    hour = ts.hour
    if OPERATING_END_HOUR >= 24:
        if hour >= OPERATING_START_HOUR:
            return ts
        return dt.datetime.combine(ts.date(), dt.time(hour=OPERATING_START_HOUR, minute=0))

    if OPERATING_START_HOUR <= hour < OPERATING_END_HOUR:
        return ts

    if hour < OPERATING_START_HOUR:
        return dt.datetime.combine(ts.date(), dt.time(hour=OPERATING_START_HOUR, minute=0))
    return dt.datetime.combine(ts.date() + dt.timedelta(days=1), dt.time(hour=OPERATING_START_HOUR, minute=0))


def generate_campus_requests(
    days: int,
    seed: int,
    now: dt.datetime,
    demand_scale: float = 2.0,
    slots: int = CAMPUS_REFERENCE_SLOTS,
) -> list[ChargeRequest]:
    rng = random.Random(seed)
    start_date = (now - dt.timedelta(days=days)).date()
    requests: list[ChargeRequest] = []

    for day_offset in range(days):
        day = start_date + dt.timedelta(days=day_offset)
        weekday = day.weekday() < 5

        # Campus baseline traffic profile calibrated for 14 slots.
        if weekday:
            base_count = max(70, int(rng.gauss(CAMPUS_WEEKDAY_BASE_ARRIVALS, 12)))
        else:
            base_count = max(45, int(rng.gauss(CAMPUS_WEEKEND_BASE_ARRIVALS, 10)))

        slot_scale = max(0.3, float(slots) / float(CAMPUS_REFERENCE_SLOTS))
        daily_count = max(1, int(round(base_count * slot_scale * demand_multiplier_from_level(demand_scale))))

        for _ in range(daily_count):
            hour = weighted_hour(rng, weekday)
            minute = rng.choice((0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55))
            arrival = dt.datetime.combine(day, dt.time(hour=hour, minute=minute))
            if arrival >= now:
                continue

            mode = "auto" if rng.random() < 0.68 else "manual"
            manual = rng.choice(MANUAL_MINUTES_OPTIONS) if mode == "manual" else None
            initial_soc = round(rng.uniform(10.0, 55.0), 2)
            duration_factor = rng.uniform(0.92, 1.08)
            demand_draw = rng.random()
            attract_draw = rng.random()

            requests.append(
                ChargeRequest(
                    arrival=arrival,
                    initial_soc=initial_soc,
                    mode=mode,
                    manual_minutes=manual,
                    duration_factor=duration_factor,
                    demand_draw=demand_draw,
                    attract_draw=attract_draw,
                )
            )

    requests.sort(key=lambda r: r.arrival)
    return requests


def build_hourly_forecast(requests: list[ChargeRequest], days: int, slots: int) -> dict[int, float]:
    by_hour = Counter(r.arrival.hour for r in requests)
    hourly_flow: dict[int, float] = {}
    for hour in range(24):
        avg_sessions = by_hour.get(hour, 0) / max(days, 1)
        hourly_flow[hour] = clamp(avg_sessions / max(slots, 1), 0.0, 1.0)
    return hourly_flow


def cap_from_load_score(load_score: float) -> int:
    load_score = clamp(load_score, 0.0, 1.0)
    if load_score >= 0.96:
        return 60
    if load_score >= 0.90:
        return 70
    if load_score >= 0.82:
        return 80
    if load_score >= 0.68:
        return 90
    return 100


def tighten_soc_cap(cap: int, levels: int = 1) -> int:
    cap = int(cap)
    if cap <= SOC_CAP_TIERS[-1]:
        return SOC_CAP_TIERS[-1]
    idx = 0
    for i, tier in enumerate(SOC_CAP_TIERS):
        if cap >= tier:
            idx = i
            break
    return SOC_CAP_TIERS[min(len(SOC_CAP_TIERS) - 1, idx + max(0, int(levels)))]


def dynamic_soc_cap(
    occupancy_ratio: float,
    queue_ratio: float,
    current_hour_flow: float,
    next_hour_flow: float,
    release_ratio: float,
) -> int:
    load_score = clamp(0.50 * occupancy_ratio + 0.30 * next_hour_flow + 0.20 * queue_ratio, 0.0, 1.0)
    no_improvement = occupancy_ratio >= 0.97 and release_ratio < 0.26 and (
        queue_ratio >= 0.30 or current_hour_flow >= 0.76 or next_hour_flow >= 0.72
    )
    severe_pressure = occupancy_ratio >= 0.94 and queue_ratio >= 0.22 and max(current_hour_flow, next_hour_flow) >= 0.90

    if no_improvement or severe_pressure:
        cap = 60
    else:
        cap = cap_from_load_score(load_score)

    if cap > 60 and release_ratio < 0.06:
        cap = tighten_soc_cap(cap, levels=1)
    return cap


def optimized_price(
    previous_price: float | None,
    occupancy_ratio: float,
    queue_ratio: float,
    release_ratio: float,
    current_hour_flow: float,
    next_hour_flow: float,
    hour: int,
    is_weekday: bool,
) -> tuple[float, float]:
    current_pressure = clamp(0.75 * occupancy_ratio + 0.25 * queue_ratio, 0.0, 1.0)
    future_pressure = clamp(max(current_hour_flow, next_hour_flow), 0.0, 1.0)
    demand_level = clamp(0.70 * current_pressure + 0.30 * future_pressure, 0.0, 1.0)

    is_peak = is_peak_hour_by_parts(hour, is_weekday)
    demand_adjust = 0.46 * (demand_level - 0.50)
    forecast_adjust = 0.14 * (future_pressure - 0.45)
    queue_adjust = 0.14 * max(0.0, queue_ratio - 0.15)
    peak_adjust = 0.10 if is_peak and demand_level >= 0.35 else 0.0
    release_adjust = 0.06 * max(0.0, 0.50 - release_ratio)
    low_load_discount = -0.22 * max(0.0, 0.52 - current_pressure)
    base_discount = -0.12 if demand_level < 0.40 and not is_peak else 0.0

    raw = BASE_PRICE_PER_KWH * (
        1.0
        + demand_adjust
        + forecast_adjust
        + queue_adjust
        + peak_adjust
        + release_adjust
        + low_load_discount
        + base_discount
    )

    smoothed = raw
    if previous_price is not None:
        alpha = 0.35
        step_limit = 0.18
        smoothed = (1.0 - alpha) * previous_price + alpha * raw
        smoothed = previous_price + clamp(smoothed - previous_price, -step_limit, step_limit)

    if is_peak:
        final = clamp(smoothed, BASE_PRICE_PER_KWH * 1.10, BASE_PRICE_PER_KWH * 1.90)
    else:
        final = clamp(smoothed, BASE_PRICE_PER_KWH * 0.70, BASE_PRICE_PER_KWH * 0.95)
    return round(final, 2), demand_level


def simulate_strategy(
    requests: list[ChargeRequest],
    slots: int,
    days: int,
    hourly_flow: dict[int, float],
    sim_start: dt.datetime,
    sim_end: dt.datetime,
    optimized: bool,
    price_elasticity: float = 0.0,
    demand_scale: float = 2.0,
) -> dict:
    slot_heap = [requests[0].arrival for _ in range(slots)] if requests else []
    heapq.heapify(slot_heap)

    sessions: list[ChargeSession] = []
    last_price: float | None = None
    demand_levels: list[float] = []
    dropped_by_price = 0
    attracted_by_price = 0
    dropped_by_capacity = 0
    effective_demand_total = len(requests)
    congestion_factor = clamp((float(demand_scale) - 1.0) / 3.0, 0.0, 1.0)
    attraction_scale = 1.0 - 0.80 * congestion_factor
    max_attraction_count = (
        max(1, int(round(0.27 * len(requests) * attraction_scale))) if optimized else 0
    )
    attraction_by_hour = Counter()
    max_attraction_per_hour = max(1, int(round(days * 2.2 * attraction_scale)))

    def schedule_one(
        arrival: dt.datetime,
        initial_soc: float,
        mode: str,
        manual_minutes: int | None,
        duration_factor: float,
    ) -> tuple[float, float, float]:
        nonlocal last_price

        earliest_slot_time = heapq.heappop(slot_heap)
        start_time = arrival if arrival >= earliest_slot_time else earliest_slot_time
        # Keep both baseline and optimized schedules inside operating hours.
        start_time = clamp_to_operating_arrival(start_time)

        wait_minutes = max(0.0, (start_time - arrival).total_seconds() / 60.0)

        occupied = sum(1 for t in slot_heap if t > start_time)
        occupancy_ratio = occupied / max(slots, 1)
        queue_ratio = clamp(wait_minutes / 45.0, 0.0, 1.0)
        lookahead = start_time + dt.timedelta(minutes=45)
        releasable = 1 + sum(1 for t in slot_heap if t <= lookahead)  # +1 for current free slot
        release_ratio = clamp(releasable / max(slots, 1), 0.0, 1.0)

        current_flow = hourly_flow.get(start_time.hour, 0.2)
        next_flow = hourly_flow.get((start_time.hour + 1) % 24, current_flow)

        if optimized:
            cap_soc = dynamic_soc_cap(
                occupancy_ratio=occupancy_ratio,
                queue_ratio=queue_ratio,
                current_hour_flow=current_flow,
                next_hour_flow=next_flow,
                release_ratio=release_ratio,
            )
            unit_price, demand_level = optimized_price(
                previous_price=last_price,
                occupancy_ratio=occupancy_ratio,
                queue_ratio=queue_ratio,
                release_ratio=release_ratio,
                current_hour_flow=current_flow,
                next_hour_flow=next_flow,
                hour=start_time.hour,
                is_weekday=(start_time.weekday() < 5),
            )
            last_price = unit_price
        else:
            cap_soc = 100
            unit_price = BASE_PRICE_PER_KWH
            demand_level = 0.0

        max_minutes = estimate_charge_minutes(initial_soc, cap_soc)
        if mode == "manual":
            planned_minutes = min(float(manual_minutes or 30), float(max_minutes))
        else:
            planned_minutes = float(max_minutes)
        planned_minutes = max(6.0, planned_minutes)

        actual_minutes = clamp(planned_minutes * duration_factor, 6.0, 150.0)
        final_soc = soc_after_minutes(initial_soc, actual_minutes, cap_soc)

        end_time = start_time + dt.timedelta(minutes=actual_minutes)
        heapq.heappush(slot_heap, end_time)

        delta_soc = max(0.0, final_soc - initial_soc)
        energy_kwh = round(BATTERY_CAPACITY_KWH * delta_soc / 100.0, 3)
        total_cost = round(energy_kwh * unit_price, 2)

        sessions.append(
            ChargeSession(
                arrival=arrival,
                start=start_time,
                end=end_time,
                wait_minutes=round(wait_minutes, 2),
                planned_minutes=round(planned_minutes, 2),
                actual_minutes=round(actual_minutes, 2),
                initial_soc=initial_soc,
                final_soc=final_soc,
                cap_soc=cap_soc,
                unit_price=unit_price,
                energy_kwh=energy_kwh,
                total_cost=total_cost,
            )
        )
        demand_levels.append(demand_level)
        return occupancy_ratio, queue_ratio, unit_price

    pending_requests: list[tuple[dt.datetime, int, ChargeRequest, int]] = []
    seq = 0
    for req in requests:
        heapq.heappush(pending_requests, (req.arrival, seq, req, 0))
        seq += 1

    while pending_requests:
        arrival, _, req, defer_rounds = heapq.heappop(pending_requests)

        if arrival >= sim_end:
            # Heap is ordered by arrival; remaining requests are also outside window.
            dropped_by_capacity += 1 + len(pending_requests)
            break

        projected_start = max(arrival, slot_heap[0]) if slot_heap else arrival
        if projected_start >= sim_end:
            # Once earliest possible start is outside window, all remaining requests
            # will also be outside the reporting window.
            dropped_by_capacity += 1 + len(pending_requests)
            break

        if optimized:
            ref_price = last_price if last_price is not None else BASE_PRICE_PER_KWH
            price_gap = (ref_price - BASE_PRICE_PER_KWH) / BASE_PRICE_PER_KWH

            if price_gap >= 0.0:
                # High-price response: postpone request to a later (typically less busy) period.
                accept_rate = clamp(1.0 - price_elasticity * price_gap, 0.50, 1.00)
                is_peak_arrival = is_peak_hour(arrival)
                peak_shift = is_peak_arrival and req.demand_draw < clamp(0.12 + 0.28 * price_elasticity, 0.12, 0.28)
                if (req.demand_draw > accept_rate or peak_shift) and defer_rounds < 3:
                    dropped_by_price += 1
                    base_delay = (90 + 40 * defer_rounds) if is_peak_arrival else (28 + 28 * defer_rounds)
                    peak_bias = 20 if is_peak_arrival else 0
                    price_bias = int(round(80 * clamp(price_gap, 0.0, 1.2)))
                    jitter = int(round(30 * req.demand_draw))
                    delay_minutes = int(clamp(base_delay + peak_bias + price_bias + jitter, 20, 240))
                    deferred_arrival = clamp_to_operating_arrival(arrival + dt.timedelta(minutes=delay_minutes))
                    heapq.heappush(pending_requests, (deferred_arrival, seq, req, defer_rounds + 1))
                    seq += 1
                    continue

        occupancy_ratio, queue_ratio, unit_price = schedule_one(
            arrival=arrival,
            initial_soc=req.initial_soc,
            mode=req.mode,
            manual_minutes=req.manual_minutes,
            duration_factor=req.duration_factor,
        )

        if optimized:
            # Low-load attraction: when slot pressure is low, discounted price can pull
            # latent campus demand into the system.
            idle_room = max(0.0, 0.68 - occupancy_ratio)
            queue_relief = max(0.0, 0.22 - queue_ratio)
            discount_ratio = max(0.0, (BASE_PRICE_PER_KWH - unit_price) / BASE_PRICE_PER_KWH)
            scheduled_hour = sessions[-1].start.hour
            is_weekday_hour = sessions[-1].start.weekday() < 5
            is_valley_window = is_shoulder_hour(scheduled_hour, is_weekday_hour)
            scheduled_hour_flow = hourly_flow.get(scheduled_hour, 0.0)
            can_attract = (
                is_valley_window
                and not is_peak_hour_by_parts(scheduled_hour, is_weekday_hour)
                and queue_ratio <= 0.20
                and occupancy_ratio <= 0.70
                and discount_ratio >= 0.04
                and 0.16 <= scheduled_hour_flow <= 0.64
                and attracted_by_price < max_attraction_count
                and attraction_by_hour[scheduled_hour] < max_attraction_per_hour
            )
            extra_prob = 0.0
            if can_attract:
                extra_prob = clamp(
                    (
                        0.75 * idle_room
                        + 0.32 * queue_relief
                        + 2.20 * price_elasticity * discount_ratio
                    )
                    * attraction_scale,
                    0.0,
                    0.36,
                )
            if can_attract and req.attract_draw < extra_prob:
                attracted_by_price += 1
                attraction_by_hour[scheduled_hour] += 1
                effective_demand_total += 1
                extra_arrival = clamp_to_operating_arrival(arrival + dt.timedelta(minutes=5))
                extra_projected_start = max(extra_arrival, slot_heap[0]) if slot_heap else extra_arrival
                if extra_projected_start < sim_end:
                    schedule_one(
                        arrival=extra_arrival,
                        initial_soc=round(clamp(req.initial_soc - 6.0, 8.0, 70.0), 2),
                        mode=req.mode,
                        manual_minutes=req.manual_minutes,
                        duration_factor=clamp(req.duration_factor * 0.97, 0.88, 1.05),
                    )
                else:
                    dropped_by_capacity += 1

    if not sessions:
        return {
            "charging_count": 0,
            "served_ratio_pct": 0.0,
            "dropped_by_price_count": int(dropped_by_price),
            "attracted_by_price_count": int(attracted_by_price),
            "avg_duration_minutes": 0.0,
            "avg_wait_minutes": 0.0,
            "p95_wait_minutes": 0.0,
            "queue_hit_rate_pct": 0.0,
            "time_utilization_pct": 0.0,
            "avg_price_cny_per_kwh": 0.0,
            "avg_peak_price_cny_per_kwh": 0.0,
            "avg_offpeak_price_cny_per_kwh": 0.0,
            "total_energy_kwh": 0.0,
            "total_revenue_cny": 0.0,
            "avg_final_soc_pct": 0.0,
            "avg_demand_level_pct": 0.0,
            "cap_distribution": {},
            "start_hour_distribution": {str(h): 0 for h in range(24)},
        }

    wait_values = sorted(s.wait_minutes for s in sessions)
    p95_idx = int(math.floor(0.95 * (len(wait_values) - 1)))
    total_minutes_operating = 0.0
    for s in sessions:
        clip_start = max(s.start, sim_start)
        clip_end = min(s.end, sim_end)
        if clip_end > clip_start:
            total_minutes_operating += operating_window_minutes(clip_start, clip_end)

    sim_days = max(1e-9, (sim_end - sim_start).total_seconds() / 86400.0)
    capacity_minutes = slots * sim_days * max(1, (OPERATING_END_HOUR - OPERATING_START_HOUR)) * 60.0
    time_utilization = 0.0 if capacity_minutes <= 0 else (total_minutes_operating / capacity_minutes) * 100.0

    cap_dist = Counter(s.cap_soc for s in sessions)
    cap_distribution = {str(k): cap_dist.get(k, 0) for k in SOC_CAP_TIERS}
    hour_dist = Counter(s.start.hour for s in sessions)
    start_hour_distribution = {str(h): int(hour_dist.get(h, 0)) for h in range(24)}
    peak_prices = [s.unit_price for s in sessions if is_peak_hour(s.start)]
    offpeak_prices = [
        s.unit_price
        for s in sessions
        if (not is_peak_hour(s.start)) and (OPERATING_START_HOUR <= s.start.hour < OPERATING_END_HOUR)
    ]

    return {
        "charging_count": len(sessions),
        "served_ratio_pct": round(100.0 * len(sessions) / max(effective_demand_total, 1), 2),
        "dropped_by_price_count": int(dropped_by_price),
        "attracted_by_price_count": int(attracted_by_price),
        "avg_duration_minutes": round(mean(s.actual_minutes for s in sessions), 2),
        "avg_wait_minutes": round(mean(s.wait_minutes for s in sessions), 2),
        "p95_wait_minutes": round(wait_values[p95_idx], 2),
        "queue_hit_rate_pct": round(100.0 * sum(1 for s in sessions if s.wait_minutes > 0.01) / len(sessions), 2),
        "time_utilization_pct": round(time_utilization, 2),
        "avg_price_cny_per_kwh": round(mean(s.unit_price for s in sessions), 3),
        "avg_peak_price_cny_per_kwh": round(mean(peak_prices), 3) if peak_prices else 0.0,
        "avg_offpeak_price_cny_per_kwh": round(mean(offpeak_prices), 3) if offpeak_prices else 0.0,
        "total_energy_kwh": round(sum(s.energy_kwh for s in sessions), 2),
        "total_revenue_cny": round(sum(s.total_cost for s in sessions), 2),
        "avg_final_soc_pct": round(mean(s.final_soc for s in sessions), 2),
        "avg_demand_level_pct": round(100.0 * mean(demand_levels), 2),
        "cap_distribution": cap_distribution,
        "start_hour_distribution": start_hour_distribution,
    }


def compare_metrics(baseline: dict, optimized: dict) -> list[dict]:
    keys = [
        ("charging_count", "Charging Sessions"),
        ("served_ratio_pct", "Demand Served Ratio (%)"),
        ("dropped_by_price_count", "Price-driven Deferrals"),
        ("attracted_by_price_count", "Price-driven Attraction"),
        ("avg_duration_minutes", "Avg Duration (min)"),
        ("avg_wait_minutes", "Avg Wait (min)"),
        ("p95_wait_minutes", "P95 Wait (min)"),
        ("queue_hit_rate_pct", "Queue Hit Rate (%)"),
        ("time_utilization_pct", "Time Utilization (%)"),
        ("avg_price_cny_per_kwh", "Avg Price (CNY/kWh)"),
        ("avg_peak_price_cny_per_kwh", "Avg Peak Price (CNY/kWh)"),
        ("avg_offpeak_price_cny_per_kwh", "Avg Off-peak Price (CNY/kWh)"),
        ("total_energy_kwh", "Total Energy (kWh)"),
        ("total_revenue_cny", "Total Revenue (CNY)"),
        ("avg_final_soc_pct", "Avg Final SOC (%)"),
    ]
    rows = []
    for key, label in keys:
        b = float(baseline.get(key, 0.0))
        o = float(optimized.get(key, 0.0))
        diff = o - b
        pct = 0.0 if abs(b) < 1e-9 else (diff / b) * 100.0
        rows.append(
            {
                "metric": label,
                "baseline": round(b, 4),
                "optimized": round(o, 4),
                "delta": round(diff, 4),
                "delta_pct": round(pct, 2),
            }
        )
    return rows


def print_report(rows: list[dict], baseline: dict, optimized: dict, slots: int, days: int, seed: int, demand_scale: float):
    print(f"Simulation completed: slots={slots}, days={days}, seed={seed}, congestion_level={demand_scale}")
    print("-" * 96)
    print(f"{'Metric':30} {'Baseline':>12} {'Optimized':>12} {'Delta':>12} {'Delta%':>10}")
    print("-" * 96)
    for r in rows:
        print(
            f"{r['metric'][:30]:30} "
            f"{r['baseline']:>12.2f} "
            f"{r['optimized']:>12.2f} "
            f"{r['delta']:>12.2f} "
            f"{r['delta_pct']:>9.2f}%"
        )
    print("-" * 96)
    print("Baseline cap distribution:", baseline.get("cap_distribution"))
    print("Optimized cap distribution:", optimized.get("cap_distribution"))


def save_outputs(rows: list[dict], baseline: dict, optimized: dict, output_dir: Path, model_info: dict):
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "sim_50_slots_compare.json"
    csv_path = output_dir / "sim_50_slots_compare.csv"

    payload = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model_info,
        "baseline": baseline,
        "optimized": optimized,
        "comparison": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", "baseline", "optimized", "delta", "delta_pct"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved JSON report: {json_path}")
    print(f"Saved CSV report:  {csv_path}")


def main():
    parser = argparse.ArgumentParser(description="Simulate campus charging and compare baseline vs optimized.")
    parser.add_argument("--slots", type=int, default=14, help="Number of slots (default: 14)")
    parser.add_argument("--days", type=int, default=30, help="Simulation days (default: 30)")
    parser.add_argument("--seed", type=int, default=20260321, help="Random seed (default: 20260321)")
    parser.add_argument(
        "--demand-scale",
        type=float,
        default=2.0,
        help="Congestion level in [1,4] (default: 2.0 for typical campus load)",
    )
    parser.add_argument(
        "--price-elasticity",
        type=float,
        default=0.4,
        help="Price elasticity coefficient e in accept_rate = 1 - e*(P-P0)/P0 (default: 0.4)",
    )
    parser.add_argument("--output-dir", type=str, default="static", help="Report output directory (default: static)")
    args = parser.parse_args()

    now = dt.datetime.now().replace(second=0, microsecond=0)
    requests = generate_campus_requests(
        days=args.days,
        seed=args.seed,
        now=now,
        demand_scale=args.demand_scale,
        slots=args.slots,
    )
    sim_start = dt.datetime.combine((now - dt.timedelta(days=args.days)).date(), dt.time(hour=0, minute=0))
    sim_end = sim_start + dt.timedelta(days=args.days)
    hourly_flow = build_hourly_forecast(requests=requests, days=args.days, slots=args.slots)

    baseline = simulate_strategy(
        requests=requests,
        slots=args.slots,
        days=args.days,
        hourly_flow=hourly_flow,
        sim_start=sim_start,
        sim_end=sim_end,
        optimized=False,
        price_elasticity=0.0,
        demand_scale=args.demand_scale,
    )

    optimized = simulate_strategy(
        requests=requests,
        slots=args.slots,
        days=args.days,
        hourly_flow=hourly_flow,
        sim_start=sim_start,
        sim_end=sim_end,
        optimized=True,
        price_elasticity=max(0.0, args.price_elasticity),
        demand_scale=args.demand_scale,
    )

    rows = compare_metrics(baseline, optimized)
    print_report(rows, baseline, optimized, slots=args.slots, days=args.days, seed=args.seed, demand_scale=args.demand_scale)
    model_info = {
        "price_elasticity_formula": "high-price: if draw>accept then defer request (up to 3 rounds), accept=clamp(1-e*(P-P0)/P0,50%,100%), all deferred arrivals are clamped to operating hours; low-load attraction probability = clamp((0.75*idle + 0.32*queue_relief + 2.20*e*discount)*attraction_scale,0,36%), where attraction_scale decreases as congestion level increases",
        "price_elasticity_e": round(max(0.0, args.price_elasticity), 4),
        "min_accept_rate": 0.50,
        "max_attraction_probability": 0.36,
        "pricing_target": "Peak in [1.10,1.90]*baseline, Off-peak in [0.70,0.95]*baseline",
        "base_price_p0": BASE_PRICE_PER_KWH,
        "utilization_window": f"{OPERATING_START_HOUR:02d}:00-{OPERATING_END_HOUR:02d}:00",
        "utilization_definition": "Time Utilization(%) = occupied charging minutes during operating hours / total available slot-minutes in operating hours",
        "core_policy_aligned_with_backend": True,
        "simulation_only_layer": "price elasticity demand deferral/attraction",
        "congestion_level_definition": "Level 1/2/3/4 -> demand multiplier 0.75/1.00/1.35/1.80",
        "campus_reference_slots": CAMPUS_REFERENCE_SLOTS,
        "campus_baseline_arrivals_weekday": CAMPUS_WEEKDAY_BASE_ARRIVALS,
        "campus_baseline_arrivals_weekend": CAMPUS_WEEKEND_BASE_ARRIVALS,
    }
    save_outputs(rows, baseline, optimized, output_dir=Path(args.output_dir), model_info=model_info)


if __name__ == "__main__":
    main()
