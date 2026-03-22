from flask import Flask, render_template, request, jsonify
import sqlite3
import datetime
import os
import random
import math
import calendar
import json
import tempfile
import subprocess

app = Flask(__name__)

DB_PATH = "parking.db"
TOTAL_SLOTS = 4
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
BASE_PRICE_PER_KWH = 1.20
BATTERY_CAPACITY_KWH = 60.0
PRICE_SNAPSHOT_INTERVAL_MINUTES = 5
MANUAL_MINUTES_OPTIONS = {30, 60, 90, 120}
SOC_CAP_TIERS = [100, 90, 80, 70, 60]
PRICE_SMOOTHING_ALPHA = 0.35
PRICE_STEP_CHANGE_LIMIT = 0.18
PEAK_HOURS = {8, 9, 10, 12, 13, 17, 18, 19, 20}
OPERATING_START_HOUR = 6
OPERATING_END_HOUR = 24
OPERATING_HOUR_FLOW_FALLBACK = 0.10

# Piecewise charging curve (minutes)
# 10% -> 60% ~ 20 min
# 60% -> 80% ~ 30 min
# 80% -> 100% ~ 45 min
CHARGE_SEGMENTS = [
    (0.0, 10.0, 5.0),
    (10.0, 60.0, 20.0),
    (60.0, 80.0, 30.0),
    (80.0, 100.0, 45.0),
]

# Ensure static directory exists
if not os.path.exists("static"):
    os.makedirs("static")

# Global environment data
current_temp = "--"
current_hum = "--"


def now_dt():
    return datetime.datetime.now()


def fmt_dt(dt_obj):
    return dt_obj.strftime(DATETIME_FMT)


def parse_dt(dt_str):
    if not dt_str:
        return None
    try:
        return datetime.datetime.strptime(dt_str, DATETIME_FMT)
    except ValueError:
        try:
            return datetime.datetime.fromisoformat(dt_str.replace("Z", ""))
        except ValueError:
            return None


def clamp(value, low, high):
    return max(low, min(high, value))


def minutes_between(start_dt, end_dt):
    return (end_dt - start_dt).total_seconds() / 60.0


def operating_window_minutes(start_dt, end_dt):
    if not start_dt or not end_dt or end_dt <= start_dt:
        return 0.0

    total = 0.0
    day = start_dt.date()
    last_day = end_dt.date()
    while day <= last_day:
        win_start = datetime.datetime.combine(day, datetime.time(hour=OPERATING_START_HOUR, minute=0))
        if OPERATING_END_HOUR >= 24:
            win_end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(hour=0, minute=0))
        else:
            win_end = datetime.datetime.combine(day, datetime.time(hour=OPERATING_END_HOUR, minute=0))

        seg_start = max(start_dt, win_start)
        seg_end = min(end_dt, win_end)
        if seg_end > seg_start:
            total += minutes_between(seg_start, seg_end)
        day += datetime.timedelta(days=1)
    return total


def get_conn(row_factory=False):
    conn = sqlite3.connect(DB_PATH)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def ensure_column(cursor, table_name, column_name, column_def):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")


def is_operating_hour(hour):
    hour = int(hour) % 24
    start = int(OPERATING_START_HOUR) % 24
    end_raw = int(OPERATING_END_HOUR)

    # End hour 24 means up to midnight on the same day.
    if end_raw >= 24:
        return hour >= start

    end = end_raw % 24
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def predict_hourly_flow(cursor, current_time, hour_offset=0):
    target_time = current_time + datetime.timedelta(hours=hour_offset)
    if not is_operating_hour(target_time.hour):
        return 0.0

    target_hour = target_time.strftime("%H")
    window_start = fmt_dt(current_time - datetime.timedelta(days=30))

    cursor.execute(
        """
        SELECT COUNT(*) AS cnt, COUNT(DISTINCT date(start_time)) AS days
        FROM charging_sessions
        WHERE start_time >= ? AND strftime('%H', start_time) = ?
        """,
        (window_start, target_hour),
    )
    row = cursor.fetchone()
    total = row["cnt"] or 0
    days = row["days"] or 0
    if days <= 0:
        return OPERATING_HOUR_FLOW_FALLBACK
    avg_sessions = total / days
    return clamp(avg_sessions / TOTAL_SLOTS, 0.0, 1.0)


def estimate_release_ratio(slot_rows, current_time, lookahead_minutes=45):
    if not slot_rows:
        return 1.0
    lookahead = current_time + datetime.timedelta(minutes=lookahead_minutes)
    releasable = 0
    for row in slot_rows:
        if not row["occupied"] and not row["license_plate"] and not row["charging"]:
            releasable += 1
            continue
        release_dt = None
        if row["charging"] and row["charge_est_end_time"]:
            release_dt = parse_dt(row["charge_est_end_time"])
        elif row["end_time"]:
            release_dt = parse_dt(row["end_time"])
        if release_dt and release_dt <= lookahead:
            releasable += 1
    return clamp(releasable / max(len(slot_rows), TOTAL_SLOTS), 0.0, 1.0)


def tighten_soc_cap(cap, levels=1):
    cap = int(cap)
    levels = max(0, int(levels))
    if cap <= SOC_CAP_TIERS[-1]:
        return SOC_CAP_TIERS[-1]

    idx = 0
    for i, tier in enumerate(SOC_CAP_TIERS):
        if cap >= tier:
            idx = i
            break

    new_idx = min(len(SOC_CAP_TIERS) - 1, idx + levels)
    return SOC_CAP_TIERS[new_idx]


def cap_from_load_score(load_score):
    load_score = clamp(float(load_score), 0.0, 1.0)
    if load_score >= 0.98:
        return 60
    if load_score >= 0.91:
        return 70
    if load_score >= 0.79:
        return 80
    if load_score >= 0.62:
        return 90
    return 100


def dynamic_soc_limit(
    occupancy_ratio,
    queue_ratio=0.0,
    current_hour_flow=0.0,
    next_hour_flow=0.0,
    release_ratio=1.0,
    no_improvement=False,
):
    occupancy_ratio = clamp(float(occupancy_ratio), 0.0, 1.0)
    queue_ratio = clamp(float(queue_ratio), 0.0, 1.0)
    current_hour_flow = clamp(float(current_hour_flow), 0.0, 1.0)
    next_hour_flow = clamp(float(next_hour_flow), 0.0, 1.0)
    release_ratio = clamp(float(release_ratio), 0.0, 1.0)

    load_score = clamp(0.50 * occupancy_ratio + 0.30 * next_hour_flow + 0.20 * queue_ratio, 0.0, 1.0)

    reason_tags = []
    if occupancy_ratio >= 1.0 and no_improvement:
        cap = 60
        reason_tags.append("full_occupancy_no_relief")
    else:
        cap = cap_from_load_score(load_score)
        reason_tags.append("load_score")

    # Minor extra protection if almost no slot will be released soon.
    if cap > 60 and release_ratio < 0.05:
        cap = tighten_soc_cap(cap, levels=1)
        reason_tags.append("low_release")

    reason = "_".join(reason_tags)
    components = [
        {
            "name": "Occupancy",
            "value": round(occupancy_ratio, 3),
        },
        {
            "name": "Queue",
            "value": round(queue_ratio, 3),
        },
        {
            "name": "Next-Hour Forecast",
            "value": round(next_hour_flow, 3),
        },
        {
            "name": "Load Score",
            "value": round(load_score, 3),
        },
        {
            "name": "45-min Release",
            "value": round(release_ratio, 3),
        },
    ]
    calculation = {
        "components": components,
        "cap_levels": [100, 90, 80, 70, 60],
        "load_score": round(load_score, 3),
    }
    return cap, reason, calculation


def estimate_charge_minutes(start_soc, target_soc):
    start_soc = clamp(float(start_soc), 0.0, 100.0)
    target_soc = clamp(float(target_soc), 0.0, 100.0)
    if target_soc <= start_soc:
        return 0.0

    total_minutes = 0.0
    for seg_start, seg_end, seg_minutes in CHARGE_SEGMENTS:
        overlap_start = max(start_soc, seg_start)
        overlap_end = min(target_soc, seg_end)
        if overlap_end <= overlap_start:
            continue
        proportion = (overlap_end - overlap_start) / (seg_end - seg_start)
        total_minutes += proportion * seg_minutes
    return round(total_minutes, 2)


def soc_after_minutes(start_soc, elapsed_minutes, cap_soc):
    start_soc = clamp(float(start_soc), 0.0, 100.0)
    cap_soc = clamp(float(cap_soc), 0.0, 100.0)
    elapsed_minutes = max(0.0, float(elapsed_minutes))

    if start_soc >= cap_soc:
        return round(cap_soc, 2)

    soc = start_soc
    remain = elapsed_minutes

    for seg_start, seg_end, seg_minutes in CHARGE_SEGMENTS:
        if soc >= cap_soc:
            break

        part_start = max(seg_start, soc)
        part_end = min(seg_end, cap_soc)
        if part_end <= part_start:
            continue

        part_span_soc = part_end - part_start
        part_span_minutes = (part_span_soc / (seg_end - seg_start)) * seg_minutes

        if remain >= part_span_minutes:
            soc = part_end
            remain -= part_span_minutes
        else:
            rate = (seg_end - seg_start) / seg_minutes
            soc = part_start + remain * rate
            remain = 0.0
            break

    return round(min(soc, cap_soc), 2)


def build_curve_points(initial_soc, cap_soc, planned_minutes, elapsed_minutes, step_minutes=5):
    planned_minutes = max(0.0, float(planned_minutes))
    elapsed_minutes = max(0.0, float(elapsed_minutes))
    if planned_minutes == 0:
        only_point = [{"minute": 0.0, "soc": round(initial_soc, 2)}]
        return only_point, only_point

    planned = []
    minute = 0.0
    while minute < planned_minutes:
        planned.append({
            "minute": round(minute, 1),
            "soc": soc_after_minutes(initial_soc, minute, cap_soc)
        })
        minute += step_minutes
    planned.append({
        "minute": round(planned_minutes, 1),
        "soc": soc_after_minutes(initial_soc, planned_minutes, cap_soc)
    })

    actual = []
    current_limit = min(elapsed_minutes, planned_minutes)
    minute = 0.0
    while minute < current_limit:
        actual.append({
            "minute": round(minute, 1),
            "soc": soc_after_minutes(initial_soc, minute, cap_soc)
        })
        minute += step_minutes
    actual.append({
        "minute": round(current_limit, 1),
        "soc": soc_after_minutes(initial_soc, current_limit, cap_soc)
    })

    return planned, actual


def calculate_occupancy_ratio(slot_rows):
    if not slot_rows:
        return 0.0
    used_count = 0
    for row in slot_rows:
        if row["occupied"] or row["license_plate"]:
            used_count += 1
    return round(used_count / max(TOTAL_SLOTS, len(slot_rows)), 4)


def build_policy_context(cursor, slot_rows, current_time, queue_len):
    occupancy_ratio = calculate_occupancy_ratio(slot_rows)
    current_hour_flow = predict_hourly_flow(cursor, current_time, hour_offset=0)
    next_hour_flow = predict_hourly_flow(cursor, current_time, hour_offset=1)
    queue_ratio = clamp(queue_len / max(TOTAL_SLOTS, 1), 0.0, 1.0)
    release_ratio = estimate_release_ratio(slot_rows, current_time, lookahead_minutes=45)
    no_improvement = (
        occupancy_ratio >= 1.0
        and release_ratio < 0.25
        and (queue_ratio >= 0.30 or current_hour_flow >= 0.78 or next_hour_flow >= 0.72)
    )
    cap, reason, calculation = dynamic_soc_limit(
        occupancy_ratio=occupancy_ratio,
        queue_ratio=queue_ratio,
        current_hour_flow=current_hour_flow,
        next_hour_flow=next_hour_flow,
        release_ratio=release_ratio,
        no_improvement=no_improvement,
    )
    return {
        "occupancy_ratio": occupancy_ratio,
        "queue_ratio": round(queue_ratio, 3),
        "current_hour_flow": round(current_hour_flow, 3),
        "next_hour_flow": round(next_hour_flow, 3),
        "release_ratio": round(release_ratio, 3),
        "no_improvement": bool(no_improvement),
        "recommended_cap": int(cap),
        "reason": reason,
        "calculation": calculation,
    }


def estimate_slot_release_minutes(slot_row, current_time):
    # Slot is immediately available
    if not slot_row["occupied"] and not slot_row["license_plate"] and not slot_row["charging"]:
        return 0

    # If charging, estimate by expected charging end time
    if slot_row["charging"] and slot_row["charge_est_end_time"]:
        est_end = parse_dt(slot_row["charge_est_end_time"])
        if est_end:
            return max(0, math.ceil(minutes_between(current_time, est_end)))

    # If reserved, estimate by reservation end time
    if slot_row["end_time"]:
        reserve_end = parse_dt(slot_row["end_time"])
        if reserve_end:
            return max(0, math.ceil(minutes_between(current_time, reserve_end)))

    # Fallback estimate
    if slot_row["occupied"]:
        return 30
    return 15


def choose_best_slot(slot_rows, current_time):
    best_slot_id = None
    best_wait = 10**9

    for row in slot_rows:
        wait_minutes = estimate_slot_release_minutes(row, current_time)
        if wait_minutes < best_wait:
            best_wait = wait_minutes
            best_slot_id = row["id"]

    if best_slot_id is None:
        return 1, 0
    return best_slot_id, max(0, best_wait)


def maybe_store_pricing_snapshot(cursor, current_time, price_data, occupancy_ratio, queue_len):
    cursor.execute("SELECT timestamp FROM pricing_snapshots ORDER BY id DESC LIMIT 1")
    last = cursor.fetchone()
    if last:
        last_dt = parse_dt(last["timestamp"])
        if last_dt:
            passed_minutes = minutes_between(last_dt, current_time)
            if passed_minutes < PRICE_SNAPSHOT_INTERVAL_MINUTES:
                return

    cursor.execute(
        """
        INSERT INTO pricing_snapshots
        (timestamp, price_per_kwh, occupancy_ratio, queue_length, current_flow, predicted_flow, weighted_flow)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fmt_dt(current_time),
            price_data["price_per_kwh"],
            round(occupancy_ratio, 4),
            int(queue_len),
            price_data["current_flow"],
            price_data["predicted_flow"],
            price_data["weighted_flow"],
        ),
    )


def calculate_dynamic_price(
    cursor,
    current_time,
    occupancy_ratio,
    queue_len,
    release_ratio=1.0,
    current_hour_flow=None,
    next_hour_flow=None,
):
    window_start = fmt_dt(current_time - datetime.timedelta(days=30))

    cursor.execute(
        """
        SELECT COUNT(*) AS cnt, COUNT(DISTINCT date(start_time)) AS days
        FROM charging_sessions
        WHERE start_time >= ?
        """,
        (window_start,),
    )
    row_total = cursor.fetchone()
    total_sessions = row_total["cnt"] or 0
    days_count = row_total["days"] or 0

    if current_hour_flow is None:
        current_hour_flow = predict_hourly_flow(cursor, current_time, hour_offset=0)
    if next_hour_flow is None:
        next_hour_flow = predict_hourly_flow(cursor, current_time, hour_offset=1)

    occupancy_ratio = clamp(float(occupancy_ratio), 0.0, 1.0)
    queue_factor = clamp(queue_len / TOTAL_SLOTS, 0.0, 1.0)
    release_ratio = clamp(float(release_ratio), 0.0, 1.0)
    current_hour_flow = clamp(float(current_hour_flow), 0.0, 1.0)
    next_hour_flow = clamp(float(next_hour_flow), 0.0, 1.0)

    # Simplified pressure model:
    # Price is centered around BASE and moves up/down by demand.
    # Goal: peak > baseline, off-peak < baseline.
    current_pressure = clamp(0.75 * occupancy_ratio + 0.25 * queue_factor, 0.0, 1.0)
    future_pressure = clamp(max(current_hour_flow, next_hour_flow), 0.0, 1.0)
    demand_level = clamp(0.70 * current_pressure + 0.30 * future_pressure, 0.0, 1.0)

    hour = current_time.hour
    is_peak = hour in PEAK_HOURS

    demand_adjust = 0.46 * (demand_level - 0.50)
    future_adjust = 0.14 * (future_pressure - 0.45)
    queue_adjust = 0.14 * max(0.0, queue_factor - 0.15)
    peak_adjust = 0.10 if is_peak and demand_level >= 0.35 else 0.0
    release_adjust = 0.06 * max(0.0, 0.50 - release_ratio)
    low_load_discount = 0.22 * max(0.0, 0.52 - current_pressure)
    extra_offpeak_discount = 0.12 if demand_level < 0.40 and not is_peak else 0.0

    base_component = BASE_PRICE_PER_KWH
    demand_component = BASE_PRICE_PER_KWH * demand_adjust
    forecast_component = BASE_PRICE_PER_KWH * future_adjust
    queue_component = BASE_PRICE_PER_KWH * queue_adjust
    peak_component = BASE_PRICE_PER_KWH * peak_adjust
    release_component = BASE_PRICE_PER_KWH * release_adjust
    discount_component = -BASE_PRICE_PER_KWH * (low_load_discount + extra_offpeak_discount)
    raw_price = (
        base_component
        + demand_component
        + forecast_component
        + queue_component
        + peak_component
        + release_component
        + discount_component
    )

    cursor.execute("SELECT price_per_kwh, timestamp FROM pricing_snapshots ORDER BY id DESC LIMIT 1")
    last_snapshot = cursor.fetchone()
    previous_price = None
    smoothed_price = raw_price
    if last_snapshot and last_snapshot["price_per_kwh"] is not None:
        previous_price = float(last_snapshot["price_per_kwh"])
        smoothed_price = (1.0 - PRICE_SMOOTHING_ALPHA) * previous_price + PRICE_SMOOTHING_ALPHA * raw_price
        delta = smoothed_price - previous_price
        smoothed_price = previous_price + clamp(delta, -PRICE_STEP_CHANGE_LIMIT, PRICE_STEP_CHANGE_LIMIT)

    if is_peak:
        min_price = BASE_PRICE_PER_KWH * 1.10
        max_price = BASE_PRICE_PER_KWH * 1.90
    else:
        min_price = BASE_PRICE_PER_KWH * 0.70
        max_price = BASE_PRICE_PER_KWH * 0.95
    price = clamp(smoothed_price, min_price, max_price)

    components = [
        {"name": "Base", "amount": round(base_component, 2)},
        {"name": "Demand", "amount": round(demand_component, 2)},
        {"name": "Forecast", "amount": round(forecast_component, 2)},
        {"name": "Queue", "amount": round(queue_component, 2)},
        {"name": "Peak", "amount": round(peak_component, 2)},
        {"name": "Release", "amount": round(release_component, 2)},
        {"name": "Discount", "amount": round(discount_component, 2)},
    ]

    return {
        "price_per_kwh": round(price, 2),
        "raw_price_per_kwh": round(raw_price, 2),
        "smoothed_price_per_kwh": round(smoothed_price, 2),
        "base_price_per_kwh": round(BASE_PRICE_PER_KWH, 2),
        "current_flow": round(current_pressure, 3),
        "predicted_flow": round(current_hour_flow, 3),
        "next_hour_predicted_flow": round(next_hour_flow, 3),
        "weighted_flow": round(demand_level, 3),
        "forecast_flow": round(future_pressure, 3),
        "queue_factor": round(queue_factor, 3),
        "release_ratio": round(release_ratio, 3),
        "is_peak": is_peak,
        "components": components,
        "summary_text": "Total = Base ± Demand ± Forecast + Queue + Peak + Release - Discount",
        "smoothing": {
            "enabled": previous_price is not None,
            "previous_price": round(previous_price, 2) if previous_price is not None else None,
            "alpha": round(PRICE_SMOOTHING_ALPHA, 2),
            "max_step": round(PRICE_STEP_CHANGE_LIMIT, 2),
        },
        "prediction_basis": "Historical profile from charging_sessions in the past 30 days, grouped by hour.",
        "historical_sessions_30d": int(total_sessions),
        "historical_days_30d": int(days_count),
    }


def finalize_charging_session(conn, slot_row, end_time, final_soc, end_reason="completed"):
    cursor = conn.cursor()

    slot_id = slot_row["id"]
    start_time = parse_dt(slot_row["charge_start_time"]) or end_time
    planned_end = parse_dt(slot_row["charge_est_end_time"]) or end_time

    actual_minutes = round(max(0.0, minutes_between(start_time, end_time)), 2)
    planned_minutes = round(max(0.0, minutes_between(start_time, planned_end)), 2)

    initial_soc = float(slot_row["charge_initial_soc"] or final_soc)
    target_soc = float(slot_row["charge_target_soc"] or final_soc)
    dynamic_cap = int(slot_row["charge_cap_soc"] or 100)
    mode = slot_row["charge_mode"] or "auto"
    manual_minutes = slot_row["charge_manual_minutes"]
    wait_minutes = float(slot_row["wait_minutes"] or 0.0)
    unit_price = float(slot_row["last_price"] or BASE_PRICE_PER_KWH)
    plate = slot_row["license_plate"]

    delta_soc = max(0.0, final_soc - initial_soc)
    energy_kwh = round(BATTERY_CAPACITY_KWH * delta_soc / 100.0, 3)
    total_cost = round(energy_kwh * unit_price, 2)

    cursor.execute(
        """
        INSERT INTO charging_sessions
        (slot_id, license_plate, start_time, end_time, initial_soc, final_soc, target_soc,
         dynamic_cap, mode, manual_minutes, planned_minutes, actual_minutes, wait_minutes,
         energy_kwh, unit_price, total_cost, end_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slot_id,
            plate,
            fmt_dt(start_time),
            fmt_dt(end_time),
            round(initial_soc, 2),
            round(final_soc, 2),
            round(target_soc, 2),
            dynamic_cap,
            mode,
            manual_minutes,
            planned_minutes,
            actual_minutes,
            round(wait_minutes, 2),
            energy_kwh,
            round(unit_price, 2),
            total_cost,
            end_reason,
        ),
    )

    cursor.execute(
        """
        UPDATE slots
        SET charging = 0,
            soc = NULL,
            charge_mode = NULL,
            charge_manual_minutes = NULL,
            charge_initial_soc = NULL,
            charge_target_soc = NULL,
            charge_cap_soc = NULL,
            charge_start_time = NULL,
            charge_est_end_time = NULL,
            last_price = NULL,
            wait_minutes = 0,
            license_plate = NULL,
            start_time = NULL,
            end_time = NULL,
            password = NULL,
            queue_request_id = NULL,
            preferred_charge_mode = NULL,
            preferred_charge_minutes = NULL
        WHERE id = ?
        """,
        (slot_id,),
    )


def auto_assign_queue(conn):
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    current_time = now_dt()

    cursor.execute("SELECT * FROM queue_entries WHERE status = 'waiting' ORDER BY id ASC")
    waiting_entries = cursor.fetchall()
    if not waiting_entries:
        return

    cursor.execute("SELECT * FROM slots ORDER BY id")
    slots = cursor.fetchall()
    free_slots = [
        s for s in slots
        if not s["occupied"] and s["license_plate"] is None and not s["charging"]
    ]
    if not free_slots:
        return

    assign_count = min(len(waiting_entries), len(free_slots))
    for i in range(assign_count):
        entry = waiting_entries[i]
        slot = free_slots[i]
        duration = int(entry["request_duration_minutes"] or 60)
        start_dt = current_time
        end_dt = current_time + datetime.timedelta(minutes=duration)

        request_time = parse_dt(entry["request_time"]) or current_time
        actual_wait = round(max(0.0, minutes_between(request_time, current_time)), 2)

        cursor.execute(
            """
            UPDATE slots
            SET license_plate = ?, start_time = ?, end_time = ?, password = ?, queue_request_id = ?,
                preferred_charge_mode = ?, preferred_charge_minutes = ?
            WHERE id = ?
            """,
            (
                entry["plate"],
                fmt_dt(start_dt),
                fmt_dt(end_dt),
                entry["pin"],
                entry["id"],
                entry["request_charge_mode"],
                entry["request_charge_minutes"],
                slot["id"],
            ),
        )

        cursor.execute(
            """
            UPDATE queue_entries
            SET status = 'assigned',
                assigned_time = ?,
                assigned_slot_id = ?,
                actual_wait_minutes = ?
            WHERE id = ?
            """,
            (fmt_dt(current_time), slot["id"], actual_wait, entry["id"]),
        )

    conn.commit()


def update_active_charging_states(conn):
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    current_time = now_dt()

    cursor.execute("SELECT * FROM slots WHERE charging = 1")
    active_slots = cursor.fetchall()
    has_update = False

    for slot in active_slots:
        start_time = parse_dt(slot["charge_start_time"])
        est_end = parse_dt(slot["charge_est_end_time"])
        initial_soc = slot["charge_initial_soc"]
        cap_soc = slot["charge_cap_soc"] if slot["charge_cap_soc"] is not None else 100
        target_soc = slot["charge_target_soc"] if slot["charge_target_soc"] is not None else cap_soc

        if start_time is None or initial_soc is None:
            continue

        elapsed_minutes = max(0.0, minutes_between(start_time, current_time))
        current_soc = soc_after_minutes(initial_soc, elapsed_minutes, cap_soc)

        finished = False
        if est_end and current_time >= est_end:
            finished = True
        if current_soc >= target_soc - 0.01:
            finished = True

        if finished:
            finalize_charging_session(
                conn=conn,
                slot_row=slot,
                end_time=current_time,
                final_soc=round(target_soc, 2),
                end_reason="completed"
            )
            has_update = True
        else:
            cursor.execute("UPDATE slots SET soc = ? WHERE id = ?", (current_soc, slot["id"]))
            has_update = True

    if has_update:
        conn.commit()

    auto_assign_queue(conn)


def init_db():
    # Ensure snapshot folder exists
    if not os.path.exists("static/snapshots"):
        os.makedirs("static/snapshots")

    # Clear old snapshots on startup
    for f in os.listdir("static/snapshots"):
        os.remove(os.path.join("static/snapshots", f))

    conn = get_conn()
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY,
            occupied INTEGER DEFAULT 0,
            charging INTEGER DEFAULT 0,
            license_plate TEXT,
            start_time TEXT,
            end_time TEXT,
            password TEXT,
            preferred_charge_mode TEXT,
            preferred_charge_minutes INTEGER,
            soc REAL,
            charge_mode TEXT,
            charge_manual_minutes INTEGER,
            charge_initial_soc REAL,
            charge_target_soc REAL,
            charge_cap_soc INTEGER,
            charge_start_time TEXT,
            charge_est_end_time TEXT,
            queue_request_id INTEGER,
            last_price REAL,
            wait_minutes REAL DEFAULT 0
        )
        """
    )

    # Backward-compatible DB column migration
    ensure_column(c, "slots", "soc", "REAL")
    ensure_column(c, "slots", "preferred_charge_mode", "TEXT")
    ensure_column(c, "slots", "preferred_charge_minutes", "INTEGER")
    ensure_column(c, "slots", "charge_mode", "TEXT")
    ensure_column(c, "slots", "charge_manual_minutes", "INTEGER")
    ensure_column(c, "slots", "charge_initial_soc", "REAL")
    ensure_column(c, "slots", "charge_target_soc", "REAL")
    ensure_column(c, "slots", "charge_cap_soc", "INTEGER")
    ensure_column(c, "slots", "charge_start_time", "TEXT")
    ensure_column(c, "slots", "charge_est_end_time", "TEXT")
    ensure_column(c, "slots", "queue_request_id", "INTEGER")
    ensure_column(c, "slots", "last_price", "REAL")
    ensure_column(c, "slots", "wait_minutes", "REAL DEFAULT 0")

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS charging_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS charging_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER,
            license_plate TEXT,
            start_time TEXT,
            end_time TEXT,
            initial_soc REAL,
            final_soc REAL,
            target_soc REAL,
            dynamic_cap INTEGER,
            mode TEXT,
            manual_minutes INTEGER,
            planned_minutes REAL,
            actual_minutes REAL,
            wait_minutes REAL,
            energy_kwh REAL,
            unit_price REAL,
            total_cost REAL,
            end_reason TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS queue_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT,
            pin TEXT,
            request_time TEXT,
            request_start_time TEXT,
            request_duration_minutes INTEGER,
            request_charge_mode TEXT,
            request_charge_minutes INTEGER,
            status TEXT DEFAULT 'waiting',
            assigned_slot_id INTEGER,
            estimated_wait_minutes REAL,
            assigned_time TEXT,
            actual_wait_minutes REAL
        )
        """
    )
    ensure_column(c, "queue_entries", "request_charge_mode", "TEXT")
    ensure_column(c, "queue_entries", "request_charge_minutes", "INTEGER")

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS pricing_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            price_per_kwh REAL,
            occupancy_ratio REAL,
            queue_length INTEGER,
            current_flow REAL,
            predicted_flow REAL,
            weighted_flow REAL
        )
        """
    )

    # Snapshot gallery table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_url TEXT,
            timestamp TEXT
        )
        """
    )

    # Keep DB snapshots aligned with folder cleanup
    c.execute("DELETE FROM snapshot_logs")

    # Initialize default slots if table is empty
    c.execute("SELECT count(*) FROM slots")
    count = c.fetchone()[0]
    if count == 0:
        for i in range(1, TOTAL_SLOTS + 1):
            c.execute(
                """
                INSERT INTO slots
                (id, occupied, charging, license_plate, start_time, end_time, password, soc, wait_minutes)
                VALUES (?, 0, 0, NULL, NULL, NULL, NULL, NULL, 0)
                """,
                (i,),
            )
    else:
        # Ensure all expected slot IDs exist
        c.execute("SELECT id FROM slots")
        existing_ids = {row[0] for row in c.fetchall()}
        for i in range(1, TOTAL_SLOTS + 1):
            if i not in existing_ids:
                c.execute(
                    """
                    INSERT INTO slots
                    (id, occupied, charging, license_plate, start_time, end_time, password, soc, wait_minutes)
                    VALUES (?, 0, 0, NULL, NULL, NULL, NULL, NULL, 0)
                    """,
                    (i,),
                )

    # Reinitialize all slots to an empty state on each startup.
    c.execute(
        """
        UPDATE slots
        SET occupied = 0,
            charging = 0,
            license_plate = NULL,
            start_time = NULL,
            end_time = NULL,
            password = NULL,
            preferred_charge_mode = NULL,
            preferred_charge_minutes = NULL,
            soc = NULL,
            charge_mode = NULL,
            charge_manual_minutes = NULL,
            charge_initial_soc = NULL,
            charge_target_soc = NULL,
            charge_cap_soc = NULL,
            charge_start_time = NULL,
            charge_est_end_time = NULL,
            queue_request_id = NULL,
            last_price = NULL,
            wait_minutes = 0
        WHERE id BETWEEN 1 AND ?
        """,
        (TOTAL_SLOTS,),
    )

    conn.commit()
    conn.close()


def collect_kpi(conn):
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    current_time = now_dt()

    # Real-time resource status
    cursor.execute("SELECT * FROM slots ORDER BY id")
    slots = cursor.fetchall()
    occupancy_ratio = calculate_occupancy_ratio(slots)
    available_slots = sum(1 for s in slots if not s["occupied"] and s["license_plate"] is None)
    active_charging = sum(1 for s in slots if s["charging"])

    cursor.execute("SELECT COUNT(*) AS c FROM queue_entries WHERE status = 'waiting'")
    queue_len = cursor.fetchone()["c"]
    policy_context = build_policy_context(cursor, slots, current_time, queue_len)

    # Total charging sessions
    cursor.execute(
        """
        SELECT COUNT(*) AS total_count,
               AVG(actual_minutes) AS avg_minutes,
               AVG(wait_minutes) AS avg_wait
        FROM charging_sessions
        """
    )
    row = cursor.fetchone()
    total_count = row["total_count"] or 0
    avg_minutes = float(row["avg_minutes"] or 0.0)
    avg_wait = float(row["avg_wait"] or 0.0)

    # Utilization in the past 24h, but only counting operational window (06:00-24:00)
    since_24h_dt = current_time - datetime.timedelta(hours=24)
    since_24h = fmt_dt(since_24h_dt)
    cursor.execute(
        "SELECT start_time, end_time, actual_minutes FROM charging_sessions WHERE end_time >= ?",
        (since_24h,),
    )
    charging_minutes_24h = 0.0
    for row_session in cursor.fetchall():
        start_dt = parse_dt(row_session["start_time"])
        end_dt = parse_dt(row_session["end_time"])
        if not start_dt or not end_dt:
            continue
        clip_start = max(start_dt, since_24h_dt)
        clip_end = min(end_dt, current_time)
        if clip_end > clip_start:
            charging_minutes_24h += operating_window_minutes(clip_start, clip_end)

    # Add elapsed minutes from active sessions
    for slot in slots:
        if slot["charging"] and slot["charge_start_time"]:
            start_dt = parse_dt(slot["charge_start_time"])
            if start_dt:
                clip_start = max(start_dt, since_24h_dt)
                clip_end = current_time
                charging_minutes_24h += operating_window_minutes(clip_start, clip_end)
    utilization_24h = 0.0
    total_capacity_minutes = TOTAL_SLOTS * max(1, (OPERATING_END_HOUR - OPERATING_START_HOUR)) * 60
    if total_capacity_minutes > 0:
        utilization_24h = clamp((charging_minutes_24h / total_capacity_minutes) * 100.0, 0.0, 100.0)

    price_data = calculate_dynamic_price(
        cursor,
        current_time,
        occupancy_ratio,
        queue_len,
        release_ratio=policy_context["release_ratio"],
        current_hour_flow=policy_context["current_hour_flow"],
        next_hour_flow=policy_context["next_hour_flow"],
    )
    maybe_store_pricing_snapshot(cursor, current_time, price_data, occupancy_ratio, queue_len)
    conn.commit()

    return {
        "charging_count": int(total_count),
        "avg_duration_minutes": round(avg_minutes, 1),
        "utilization_24h_pct": round(utilization_24h, 1),
        "utilization_window": f"{OPERATING_START_HOUR:02d}:00-{OPERATING_END_HOUR:02d}:00",
        "avg_wait_minutes": round(avg_wait, 1),
        "active_charging": int(active_charging),
        "available_slots": int(available_slots),
        "queue_length": int(queue_len),
        "occupancy_ratio": round(occupancy_ratio * 100.0, 1),
        "dynamic_soc_cap": policy_context["recommended_cap"],
        "policy_reason": policy_context["reason"],
        "next_hour_predicted_flow": policy_context["next_hour_flow"],
        "no_improvement_expected": policy_context["no_improvement"],
        "policy": {
            "occupancy_ratio": round(policy_context["occupancy_ratio"] * 100.0, 1),
            "queue_ratio": round(policy_context["queue_ratio"], 3),
            "release_ratio": round(policy_context["release_ratio"], 3),
            "current_hour_flow": policy_context["current_hour_flow"],
            "next_hour_predicted_flow": policy_context["next_hour_flow"],
            "recommended_cap": policy_context["recommended_cap"],
            "no_improvement_expected": policy_context["no_improvement"],
            "reason": policy_context["reason"],
            "calculation": policy_context["calculation"],
        },
        "pricing": price_data,
    }


def build_price_forecast(conn, hours=8):
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    current_time = now_dt()
    hours = int(clamp(hours, 1, 24))

    cursor.execute("SELECT * FROM slots ORDER BY id")
    slots = cursor.fetchall()
    base_occupancy = calculate_occupancy_ratio(slots)
    cursor.execute("SELECT COUNT(*) AS c FROM queue_entries WHERE status = 'waiting'")
    queue_len = int(cursor.fetchone()["c"] or 0)

    forecast = []
    for offset in range(1, hours + 1):
        t = current_time + datetime.timedelta(hours=offset)
        current_flow = predict_hourly_flow(cursor, t, hour_offset=0)
        next_flow = predict_hourly_flow(cursor, t, hour_offset=1)

        forecast_occupancy = clamp(0.60 * base_occupancy + 0.40 * current_flow, 0.0, 1.0)
        forecast_queue_ratio = clamp((queue_len / max(TOTAL_SLOTS, 1)) * (0.85 ** (offset - 1)), 0.0, 1.0)
        forecast_queue_len = int(round(forecast_queue_ratio * TOTAL_SLOTS))
        forecast_release = clamp(0.35 + 0.45 * (1.0 - current_flow), 0.0, 1.0)

        price_data = calculate_dynamic_price(
            cursor,
            t,
            forecast_occupancy,
            forecast_queue_len,
            release_ratio=forecast_release,
            current_hour_flow=current_flow,
            next_hour_flow=next_flow,
        )
        forecast.append(
            {
                "hour_offset": offset,
                "time_label": t.strftime("%m-%d %H:00"),
                "price_per_kwh": price_data["price_per_kwh"],
                "is_peak": price_data["is_peak"],
                "demand_level": price_data["weighted_flow"],
                "queue_factor": round(forecast_queue_ratio, 3),
                "forecast_flow": round(current_flow, 3),
            }
        )

    return {
        "generated_at": fmt_dt(current_time),
        "hours": hours,
        "basis": "Predicted from charging_sessions over the last 30 days (same-hour historical profile), weighted with current occupancy/queue.",
        "forecast": forecast,
    }


def build_history_data(conn, group):
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    current_time = now_dt()

    if group == "month":
        # Last 6 months
        months = []
        year = current_time.year
        month = current_time.month
        for _ in range(6):
            months.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                month = 12
                year -= 1
        months.reverse()

        data_map = {
            label: {
                "charging_count": 0,
                "avg_duration_minutes": 0.0,
                "avg_wait_minutes": 0.0,
                "utilization_pct": 0.0,
            }
            for label in months
        }

        window_start = f"{months[0]}-01 00:00:00"
        cursor.execute(
            """
            SELECT strftime('%Y-%m', start_time) AS period,
                   COUNT(*) AS charging_count,
                   AVG(actual_minutes) AS avg_duration_minutes,
                   AVG(wait_minutes) AS avg_wait_minutes,
                   COALESCE(SUM(actual_minutes), 0) AS sum_minutes
            FROM charging_sessions
            WHERE start_time >= ?
            GROUP BY period
            """,
            (window_start,),
        )
        rows = cursor.fetchall()
        for row in rows:
            period = row["period"]
            if period not in data_map:
                continue
            y, m = period.split("-")
            days = calendar.monthrange(int(y), int(m))[1]
            capacity_minutes = TOTAL_SLOTS * days * max(1, (OPERATING_END_HOUR - OPERATING_START_HOUR)) * 60
            util = 0.0 if capacity_minutes == 0 else (float(row["sum_minutes"] or 0.0) / capacity_minutes) * 100.0
            data_map[period] = {
                "charging_count": int(row["charging_count"] or 0),
                "avg_duration_minutes": round(float(row["avg_duration_minutes"] or 0.0), 1),
                "avg_wait_minutes": round(float(row["avg_wait_minutes"] or 0.0), 1),
                "utilization_pct": round(clamp(util, 0.0, 100.0), 1),
            }

        return {
            "group": "month",
            "labels": months,
            "series": [data_map[label] for label in months],
        }

    # Default: by day (last 14 days)
    days = []
    for i in range(13, -1, -1):
        day = (current_time - datetime.timedelta(days=i)).date()
        days.append(day.strftime("%Y-%m-%d"))

    data_map = {
        day: {
            "charging_count": 0,
            "avg_duration_minutes": 0.0,
            "avg_wait_minutes": 0.0,
            "utilization_pct": 0.0,
        }
        for day in days
    }

    window_start = f"{days[0]} 00:00:00"
    cursor.execute(
        """
        SELECT date(start_time) AS period,
               COUNT(*) AS charging_count,
               AVG(actual_minutes) AS avg_duration_minutes,
               AVG(wait_minutes) AS avg_wait_minutes,
               COALESCE(SUM(actual_minutes), 0) AS sum_minutes
        FROM charging_sessions
        WHERE start_time >= ?
        GROUP BY period
        """,
        (window_start,),
    )
    rows = cursor.fetchall()
    for row in rows:
        period = row["period"]
        if period not in data_map:
            continue
        capacity_minutes = TOTAL_SLOTS * max(1, (OPERATING_END_HOUR - OPERATING_START_HOUR)) * 60
        util = 0.0 if capacity_minutes == 0 else (float(row["sum_minutes"] or 0.0) / capacity_minutes) * 100.0
        data_map[period] = {
            "charging_count": int(row["charging_count"] or 0),
            "avg_duration_minutes": round(float(row["avg_duration_minutes"] or 0.0), 1),
            "avg_wait_minutes": round(float(row["avg_wait_minutes"] or 0.0), 1),
            "utilization_pct": round(clamp(util, 0.0, 100.0), 1),
        }

    return {
        "group": "day",
        "labels": [d[5:] for d in days],  # Display MM-DD only
        "series": [data_map[day] for day in days],
    }


@app.route("/")
def index():
    logo_path = os.path.join("static", "bjtu_logo.jpg")
    logo_version = int(os.path.getmtime(logo_path)) if os.path.exists(logo_path) else 0
    return render_template("index.html", logo_version=logo_version)


# =======================================================
# Receive K230 snapshots and build history records
# =======================================================
@app.route("/upload_frame", methods=["POST"])
def upload_frame():
    img_data = request.get_data()

    if img_data and len(img_data) > 0:
        now = now_dt()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        filename = now.strftime("%Y%m%d_%H%M%S") + ".jpg"
        filepath = os.path.join("static", "snapshots", filename)

        with open(filepath, "wb") as f:
            f.write(img_data)

        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO snapshot_logs (image_url, timestamp) VALUES (?, ?)",
            ("/static/snapshots/" + filename, time_str),
        )
        conn.commit()
        conn.close()

        print(f">>> [Snapshot Saved] {filename}")
        return "Image Saved OK", 200
    return "Failed", 400


@app.route("/api/snapshots")
def get_snapshots():
    conn = get_conn(row_factory=True)
    c = conn.cursor()
    c.execute("SELECT image_url, timestamp FROM snapshot_logs ORDER BY id DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()

    logs = [{"url": row["image_url"], "time": row["timestamp"]} for row in rows]
    return jsonify(logs)


@app.route("/api/status")
def get_status():
    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    c = conn.cursor()

    c.execute("SELECT * FROM slots ORDER BY id")
    rows = c.fetchall()
    current_time = now_dt()

    needs_cleanup = False
    for row in rows:
        row_plate = row["license_plate"]
        start_str = row["start_time"]
        end_str = row["end_time"]
        should_cancel = False

        if row_plate and start_str:
            start_dt = parse_dt(start_str)
            end_dt = parse_dt(end_str)
            if end_dt and current_time > end_dt and row["charging"] == 0:
                should_cancel = True
            if start_dt:
                timeout_limit = start_dt + datetime.timedelta(minutes=5)
                if current_time > timeout_limit and row["occupied"] == 0 and row["charging"] == 0:
                    should_cancel = True

        if should_cancel:
            c.execute(
                """
                UPDATE slots
                SET license_plate = NULL, start_time = NULL, end_time = NULL, password = NULL, queue_request_id = NULL,
                    preferred_charge_mode = NULL, preferred_charge_minutes = NULL, soc = NULL
                WHERE id = ?
                """,
                (row["id"],),
            )
            needs_cleanup = True

    if needs_cleanup:
        conn.commit()
        auto_assign_queue(conn)
        c.execute("SELECT * FROM slots ORDER BY id")
        rows = c.fetchall()

    c.execute("SELECT COUNT(*) AS c FROM queue_entries WHERE status = 'waiting'")
    queue_len = c.fetchone()["c"]
    policy_context = build_policy_context(c, rows, current_time, queue_len)

    data = []
    for row in rows:
        charge_detail = None
        current_soc = row["soc"]

        if row["charging"] and row["charge_start_time"] and row["charge_initial_soc"] is not None:
            start_dt = parse_dt(row["charge_start_time"]) or current_time
            est_end = parse_dt(row["charge_est_end_time"]) or current_time
            elapsed = max(0.0, minutes_between(start_dt, current_time))
            planned = max(0.0, minutes_between(start_dt, est_end))
            remaining = max(0.0, planned - elapsed)

            cap_soc = row["charge_cap_soc"] if row["charge_cap_soc"] is not None else 100
            target_soc = row["charge_target_soc"] if row["charge_target_soc"] is not None else cap_soc
            current_soc = soc_after_minutes(row["charge_initial_soc"], elapsed, cap_soc)

            planned_curve, actual_curve = build_curve_points(
                initial_soc=row["charge_initial_soc"],
                cap_soc=cap_soc,
                planned_minutes=planned,
                elapsed_minutes=elapsed,
            )

            charge_detail = {
                "mode": row["charge_mode"] or "auto",
                "manual_minutes": row["charge_manual_minutes"],
                "initial_soc": round(float(row["charge_initial_soc"]), 2),
                "current_soc": round(float(current_soc), 2),
                "target_soc": round(float(target_soc), 2),
                "cap_soc": int(cap_soc),
                "elapsed_minutes": round(elapsed, 1),
                "planned_minutes": round(planned, 1),
                "remaining_minutes": round(remaining, 1),
                "start_time": row["charge_start_time"],
                "est_end_time": row["charge_est_end_time"],
                "unit_price": round(float(row["last_price"] or BASE_PRICE_PER_KWH), 2),
                "planned_curve": planned_curve,
                "actual_curve": actual_curve,
            }

        data.append(
            {
                "id": row["id"],
                "occupied": bool(row["occupied"]),
                "charging": bool(row["charging"]),
                "license_plate": row["license_plate"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "preferred_charge_mode": row["preferred_charge_mode"],
                "preferred_charge_minutes": row["preferred_charge_minutes"],
                "soc": round(float(current_soc), 2) if current_soc is not None else None,
                "queue_request_id": row["queue_request_id"],
                "charge_detail": charge_detail,
            }
        )

    conn.close()
    return jsonify(
        {
            "slots": data,
            "policy": {
                "occupancy_ratio": round(policy_context["occupancy_ratio"] * 100.0, 1),
                "queue_ratio": round(policy_context["queue_ratio"], 3),
                "release_ratio": round(policy_context["release_ratio"], 3),
                "current_hour_flow": policy_context["current_hour_flow"],
                "recommended_cap": policy_context["recommended_cap"],
                "next_hour_predicted_flow": policy_context["next_hour_flow"],
                "no_improvement_expected": policy_context["no_improvement"],
                "reason": policy_context["reason"],
                "calculation": policy_context["calculation"],
            },
        }
    )


# Receive temperature/humidity data (ESP32)
@app.route("/update_env")
def update_env():
    global current_temp, current_hum
    current_temp = request.args.get("temp", "--")
    current_hum = request.args.get("hum", "--")
    return "OK"


# Frontend environment endpoint
@app.route("/api/env")
def api_env():
    return jsonify({"temp": current_temp, "hum": current_hum})


# Plate verification endpoint for ESP32
@app.route("/api/verify", methods=["POST"])
def verify_plate():
    plate_from_esp32 = request.data.decode("utf-8").strip()
    if not plate_from_esp32:
        return "ERROR: Empty Plate"

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM slots WHERE license_plate = ?", (plate_from_esp32,))
    row = c.fetchone()
    conn.close()

    if row:
        slot_id = row[0]
        return f"OK,{slot_id}"
    return "FAIL"


@app.route("/api/reserve", methods=["POST"])
def reserve_slot():
    data = request.json or {}
    slot_id = int(data.get("id", 0))
    plate = (data.get("plate") or "").strip().upper()
    action = data.get("action")
    pin = (data.get("pin") or "").strip()

    conn = get_conn(row_factory=True)
    c = conn.cursor()

    c.execute("SELECT * FROM slots WHERE id = ?", (slot_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"success": False, "message": "Slot not found"})

    if action == "reserve":
        if not plate:
            conn.close()
            return jsonify({"success": False, "message": "Please enter a plate number"})
        if not pin or len(pin) != 4 or not pin.isdigit():
            conn.close()
            return jsonify({"success": False, "message": "Please set a 4-digit PIN!"})

        try:
            start_dt = datetime.datetime.strptime(data.get("startTime"), "%Y-%m-%dT%H:%M")
        except Exception:
            conn.close()
            return jsonify({"success": False, "message": "Invalid start time"})

        charge_policy = (data.get("chargePolicy") or "auto").lower()
        charge_minutes = data.get("chargeMinutes")
        if charge_policy not in {"auto", "timed"}:
            charge_policy = "auto"

        if charge_policy == "timed":
            try:
                charge_minutes = int(charge_minutes)
            except Exception:
                charge_minutes = 30
            if charge_minutes not in MANUAL_MINUTES_OPTIONS:
                charge_minutes = 30
            duration_minutes = int(charge_minutes)
        else:
            charge_minutes = None
            duration_minutes = 120

        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)

        slot_is_available = (row["occupied"] == 0) and (row["license_plate"] is None) and (row["charging"] == 0)
        if slot_is_available:
            c.execute(
                """
                UPDATE slots
                SET license_plate = ?, start_time = ?, end_time = ?, password = ?,
                    preferred_charge_mode = ?, preferred_charge_minutes = ?
                WHERE id = ?
                """,
                (
                    plate,
                    fmt_dt(start_dt),
                    fmt_dt(end_dt),
                    pin,
                    charge_policy,
                    charge_minutes,
                    slot_id,
                ),
            )
            conn.commit()
            conn.close()
            mode_label = "Auto Stop" if charge_policy == "auto" else f"Timed ({charge_minutes} min)"
            return jsonify({"success": True, "queued": False, "message": f"Reservation confirmed. Charging mode: {mode_label}"})

        # Target slot unavailable: check whether other slots are free
        c.execute(
            "SELECT id FROM slots WHERE occupied = 0 AND license_plate IS NULL AND charging = 0 ORDER BY id"
        )
        free_slots = [r["id"] for r in c.fetchall()]
        if free_slots:
            conn.close()
            free_text = ", ".join([f"#{sid}" for sid in free_slots])
            return jsonify(
                {
                    "success": False,
                    "queued": False,
                    "message": f"Selected slot unavailable. Free slots: {free_text}",
                }
            )

        # Fully occupied: enqueue request
        current_time = now_dt()
        c.execute("SELECT * FROM slots ORDER BY id")
        all_slots = c.fetchall()
        predicted_slot_id, est_wait = choose_best_slot(all_slots, current_time)
        c.execute(
            """
            INSERT INTO queue_entries
            (plate, pin, request_time, request_start_time, request_duration_minutes,
             request_charge_mode, request_charge_minutes, status, assigned_slot_id, estimated_wait_minutes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)
            """,
            (
                plate,
                pin,
                fmt_dt(current_time),
                fmt_dt(start_dt),
                duration_minutes,
                charge_policy,
                charge_minutes,
                predicted_slot_id,
                float(est_wait),
            ),
        )
        queue_id = c.lastrowid
        conn.commit()
        conn.close()
        return jsonify(
            {
                "success": True,
                "queued": True,
                "queue_id": queue_id,
                "estimated_wait_minutes": est_wait,
                "assigned_slot_id": predicted_slot_id,
                "message": f"All slots are full. Added to queue: est. wait {est_wait} min, suggested slot #{predicted_slot_id}",
            }
        )

    if action == "cancel":
        saved_pin = row["password"]
        if saved_pin and pin != saved_pin:
            conn.close()
            return jsonify({"success": False, "message": "Incorrect PIN! Cancel failed."})

        c.execute(
            """
            UPDATE slots
            SET license_plate = NULL, start_time = NULL, end_time = NULL, password = NULL, queue_request_id = NULL,
                preferred_charge_mode = NULL, preferred_charge_minutes = NULL, soc = NULL
            WHERE id = ?
            """,
            (slot_id,),
        )
        conn.commit()
        auto_assign_queue(conn)
        conn.close()
        return jsonify({"success": True, "message": "Reservation Canceled"})

    conn.close()
    return jsonify({"success": False, "message": "Unknown action"})


@app.route("/api/queue")
def get_queue():
    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    auto_assign_queue(conn)
    c = conn.cursor()

    c.execute(
        """
        SELECT *
        FROM queue_entries
        WHERE status = 'waiting'
           OR (status = 'assigned' AND assigned_time >= ?)
        ORDER BY id DESC
        LIMIT 30
        """,
        (fmt_dt(now_dt() - datetime.timedelta(hours=2)),),
    )
    rows = c.fetchall()

    c.execute("SELECT COUNT(*) AS c FROM queue_entries WHERE status = 'waiting'")
    waiting_count = c.fetchone()["c"]

    queue_list = []
    for row in rows:
        queue_list.append(
            {
                "id": row["id"],
                "plate": row["plate"],
                "status": row["status"],
                "request_time": row["request_time"],
                "assigned_slot_id": row["assigned_slot_id"],
                "estimated_wait_minutes": row["estimated_wait_minutes"],
                "actual_wait_minutes": row["actual_wait_minutes"],
                "assigned_time": row["assigned_time"],
                "request_charge_mode": row["request_charge_mode"],
                "request_charge_minutes": row["request_charge_minutes"],
            }
        )

    conn.close()
    return jsonify({"waiting_count": int(waiting_count), "queue": queue_list})


def initialize_charging_session(
    conn,
    slot_id,
    request_mode=None,
    request_manual_minutes=None,
    allow_sim_plate=False,
    insert_log=True,
):
    c = conn.cursor()
    c.execute("SELECT * FROM slots WHERE id = ?", (slot_id,))
    slot = c.fetchone()
    if not slot:
        return {"success": False, "message": "Slot not found"}
    if slot["charging"]:
        return {"success": False, "message": "This slot is already charging"}

    # Optional demo behavior for web-triggered start on an empty slot.
    if allow_sim_plate and not slot["occupied"] and slot["license_plate"] is None:
        sim_plate = f"SIM{random.randint(1000, 9999)}"
        c.execute(
            """
            UPDATE slots
            SET license_plate = ?, start_time = ?, end_time = ?,
                preferred_charge_mode = COALESCE(preferred_charge_mode, 'auto')
            WHERE id = ?
            """,
            (
                sim_plate,
                fmt_dt(now_dt()),
                fmt_dt(now_dt() + datetime.timedelta(hours=2)),
                slot_id,
            ),
        )
        c.execute("SELECT * FROM slots WHERE id = ?", (slot_id,))
        slot = c.fetchone()

    c.execute("SELECT * FROM slots ORDER BY id")
    slots = c.fetchall()
    c.execute("SELECT COUNT(*) AS c FROM queue_entries WHERE status = 'waiting'")
    queue_len = c.fetchone()["c"]
    policy_context = build_policy_context(c, slots, now_dt(), queue_len)
    occupancy_ratio = policy_context["occupancy_ratio"]
    cap_soc = policy_context["recommended_cap"]

    preferred_mode = (slot["preferred_charge_mode"] or "").lower()
    preferred_minutes = slot["preferred_charge_minutes"]
    if preferred_mode in {"auto", "timed"}:
        if preferred_mode == "timed":
            mode = "manual"
            manual_minutes = preferred_minutes
        else:
            mode = "auto"
            manual_minutes = None
    else:
        mode = (request_mode or "auto").lower()
        manual_minutes = request_manual_minutes

    initial_soc = round(random.uniform(10, 55), 2)
    max_minutes_to_cap = estimate_charge_minutes(initial_soc, cap_soc)

    if mode == "manual":
        try:
            manual_minutes = int(manual_minutes)
        except Exception:
            manual_minutes = 30
        if manual_minutes not in MANUAL_MINUTES_OPTIONS:
            manual_minutes = 30
        planned_minutes = min(float(manual_minutes), float(max_minutes_to_cap))
    else:
        mode = "auto"
        manual_minutes = None
        planned_minutes = float(max_minutes_to_cap)

    if planned_minutes <= 0:
        planned_minutes = 1.0

    target_soc = soc_after_minutes(initial_soc, planned_minutes, cap_soc)
    start_time = now_dt()
    est_end = start_time + datetime.timedelta(minutes=planned_minutes)
    price_data = calculate_dynamic_price(
        c,
        start_time,
        occupancy_ratio,
        queue_len,
        release_ratio=policy_context["release_ratio"],
        current_hour_flow=policy_context["current_hour_flow"],
        next_hour_flow=policy_context["next_hour_flow"],
    )

    wait_minutes = 0.0
    if slot["queue_request_id"]:
        c.execute(
            "SELECT actual_wait_minutes, estimated_wait_minutes FROM queue_entries WHERE id = ?",
            (slot["queue_request_id"],),
        )
        q = c.fetchone()
        if q:
            wait_minutes = float(q["actual_wait_minutes"] or q["estimated_wait_minutes"] or 0.0)

    c.execute(
        """
        UPDATE slots
        SET charging = 1,
            soc = ?,
            charge_mode = ?,
            charge_manual_minutes = ?,
            charge_initial_soc = ?,
            charge_target_soc = ?,
            charge_cap_soc = ?,
            charge_start_time = ?,
            charge_est_end_time = ?,
            last_price = ?,
            wait_minutes = ?
        WHERE id = ?
        """,
        (
            initial_soc,
            mode,
            manual_minutes,
            initial_soc,
            target_soc,
            cap_soc,
            fmt_dt(start_time),
            fmt_dt(est_end),
            price_data["price_per_kwh"],
            round(wait_minutes, 2),
            slot_id,
        ),
    )

    if insert_log:
        c.execute("INSERT INTO charging_logs (slot_id) VALUES (?)", (slot_id,))
    maybe_store_pricing_snapshot(c, start_time, price_data, occupancy_ratio, queue_len)

    return {
        "success": True,
        "message": "Charging started",
        "slot_id": slot_id,
        "mode": mode,
        "manual_minutes": manual_minutes,
        "initial_soc": initial_soc,
        "target_soc": round(target_soc, 2),
        "cap_soc": int(cap_soc),
        "policy_reason": policy_context["reason"],
        "planned_minutes": round(planned_minutes, 1),
        "unit_price": price_data["price_per_kwh"],
    }


@app.route("/api/charge/start", methods=["POST"])
def start_charge():
    data = request.json or {}
    slot_id = int(data.get("id", 0))
    request_mode = (data.get("mode") or "auto").lower()
    request_manual_minutes = data.get("manual_minutes")

    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    result = initialize_charging_session(
        conn=conn,
        slot_id=slot_id,
        request_mode=request_mode,
        request_manual_minutes=request_manual_minutes,
        allow_sim_plate=True,
        insert_log=True,
    )
    if not result["success"]:
        conn.close()
        return jsonify(result)

    conn.commit()
    conn.close()
    return jsonify(result)


@app.route("/api/charge/stop", methods=["POST"])
def stop_charge():
    data = request.json or {}
    slot_id = int(data.get("id", 0))

    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    c = conn.cursor()
    c.execute("SELECT * FROM slots WHERE id = ?", (slot_id,))
    slot = c.fetchone()

    if not slot:
        conn.close()
        return jsonify({"success": False, "message": "Slot not found"})
    if not slot["charging"]:
        conn.close()
        return jsonify({"success": False, "message": "Slot is not charging"})

    start_time = parse_dt(slot["charge_start_time"]) or now_dt()
    elapsed = max(0.0, minutes_between(start_time, now_dt()))
    cap_soc = slot["charge_cap_soc"] if slot["charge_cap_soc"] is not None else 100
    initial_soc = slot["charge_initial_soc"] if slot["charge_initial_soc"] is not None else slot["soc"] or 10
    final_soc = soc_after_minutes(initial_soc, elapsed, cap_soc)

    finalize_charging_session(conn, slot, now_dt(), final_soc, end_reason="manual_stop")
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": "Charging stopped", "final_soc": round(final_soc, 2)})


@app.route("/api/kpi")
def get_kpi():
    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    kpi = collect_kpi(conn)
    conn.close()
    return jsonify(kpi)


@app.route("/api/price_forecast")
def get_price_forecast():
    hours = request.args.get("hours", default=8, type=int)
    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    data = build_price_forecast(conn, hours=hours if hours else 8)
    conn.close()
    return jsonify(data)


@app.route("/api/simulate_compare", methods=["POST"])
def simulate_compare():
    data = request.json or {}
    slots = 14
    try:
        days = int(data.get("days", 30))
    except Exception:
        days = 30
    try:
        demand_scale = float(data.get("demand_scale", 2.0))
    except Exception:
        demand_scale = 2.0
    try:
        seed = int(data.get("seed", 20260321))
    except Exception:
        seed = 20260321
    try:
        price_elasticity = float(data.get("price_elasticity", 0.4))
    except Exception:
        price_elasticity = 0.4

    slots = 14
    days = int(clamp(days, 1, 365))
    demand_scale = float(clamp(demand_scale, 1.0, 4.0))
    price_elasticity = float(clamp(price_elasticity, 0.0, 1.5))

    script_path = os.path.join(os.path.dirname(__file__), "scripts", "simulate_50_slots_compare.py")
    if not os.path.exists(script_path):
        return jsonify({"success": False, "message": "Simulation script not found"}), 500

    with tempfile.TemporaryDirectory(prefix="sim_compare_") as tmp_dir:
        cmd = [
            "python3",
            script_path,
            "--slots",
            str(slots),
            "--days",
            str(days),
            "--demand-scale",
            str(demand_scale),
            "--seed",
            str(seed),
            "--price-elasticity",
            str(price_elasticity),
            "--output-dir",
            tmp_dir,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=240,
            )
        except subprocess.TimeoutExpired:
            return jsonify({"success": False, "message": "Simulation timed out"}), 504
        except Exception as e:
            return jsonify({"success": False, "message": f"Simulation failed: {e}"}), 500

        if proc.returncode != 0:
            return jsonify(
                {
                    "success": False,
                    "message": "Simulation process failed",
                    "stderr": (proc.stderr or "")[-2000:],
                }
            ), 500

        report_path = os.path.join(tmp_dir, "sim_50_slots_compare.json")
        if not os.path.exists(report_path):
            return jsonify({"success": False, "message": "Simulation report not generated"}), 500

        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except Exception as e:
            return jsonify({"success": False, "message": f"Cannot parse simulation report: {e}"}), 500

    return jsonify(
        {
            "success": True,
            "config": {
                "slots": slots,
                "days": days,
                "demand_scale": demand_scale,
                "seed": seed,
                "price_elasticity": price_elasticity,
            },
            "generated_at": report.get("generated_at"),
            "model": report.get("model", {}),
            "baseline": report.get("baseline", {}),
            "optimized": report.get("optimized", {}),
            "comparison": report.get("comparison", []),
        }
    )


@app.route("/api/history")
def get_history():
    group = (request.args.get("group") or "day").lower()
    if group not in {"day", "month"}:
        group = "day"
    conn = get_conn(row_factory=True)
    data = build_history_data(conn, group)
    conn.close()
    return jsonify(data)


@app.route("/api/stats")
def get_stats():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT strftime('%H', start_time) AS hour, COUNT(*)
        FROM charging_sessions
        GROUP BY hour
        ORDER BY hour
        """
    )
    rows = c.fetchall()
    conn.close()

    hours_data = {str(i).zfill(2): 0 for i in range(24)}
    for row in rows:
        if row[0] is not None:
            hours_data[row[0]] = row[1]
    return jsonify(list(hours_data.values()))


@app.route("/update", methods=["GET"])
def update_slot():
    slot_id = request.args.get("id", type=int)
    occupied = request.args.get("occupied")
    charging = request.args.get("charging")
    response_msg = "OK"

    if not slot_id:
        return "Error"

    conn = get_conn(row_factory=True)
    update_active_charging_states(conn)
    c = conn.cursor()

    c.execute("SELECT * FROM slots WHERE id = ?", (slot_id,))
    slot = c.fetchone()
    if not slot:
        conn.close()
        return "Error"

    if occupied is not None:
        occ_val = 1 if int(occupied) else 0
        prev_occ = int(slot["occupied"] or 0)
        c.execute("UPDATE slots SET occupied = ? WHERE id = ?", (occ_val, slot_id))

        if occ_val == 1 and prev_occ == 0:
            c.execute("SELECT license_plate FROM slots WHERE id = ?", (slot_id,))
            row = c.fetchone()
            if row and row["license_plate"] is None:
                response_msg = "ALARM"
        elif occ_val == 0:
            c.execute("SELECT charging, license_plate FROM slots WHERE id = ?", (slot_id,))
            row = c.fetchone()
            if row and int(row["charging"] or 0) == 0 and row["license_plate"] is None:
                c.execute("UPDATE slots SET soc = NULL WHERE id = ?", (slot_id,))

    if charging is not None:
        incoming = 1 if int(charging) else 0
        previous = int(slot["charging"] or 0)

        if incoming == 1 and previous == 0:
            init_result = initialize_charging_session(
                conn=conn,
                slot_id=slot_id,
                request_mode=None,
                request_manual_minutes=None,
                allow_sim_plate=False,
                insert_log=True,
            )
            if not init_result["success"] and init_result["message"] != "This slot is already charging":
                response_msg = "Error"

        elif incoming == 0 and previous == 1 and slot["charge_start_time"]:
            start_dt = parse_dt(slot["charge_start_time"]) or now_dt()
            elapsed = max(0.0, minutes_between(start_dt, now_dt()))
            cap_soc = slot["charge_cap_soc"] if slot["charge_cap_soc"] is not None else 100
            initial_soc = slot["charge_initial_soc"] if slot["charge_initial_soc"] is not None else (slot["soc"] or 10)
            final_soc = soc_after_minutes(initial_soc, elapsed, cap_soc)
            finalize_charging_session(conn, slot, now_dt(), final_soc, end_reason="hardware_stop")

        else:
            c.execute("UPDATE slots SET charging = ? WHERE id = ?", (incoming, slot_id))

    conn.commit()
    auto_assign_queue(conn)
    conn.close()
    return response_msg


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5001)
