"""SimPy discrete-event simulation of a serial production line with finite buffers (blocking / starving).

Modelling choices (all documented in docs/MODEL_CARD.md):
* Entities are LOTS of `lot_units` units (keeps event counts small). Service time of a lot = lot_units * cycle_sec * speed
  / (parallelism * capacity) * lognormal noise (mean 1, CV `cv`).
* MES downtime is recorded per LINE-hour and behaves as a whole-line stop (partial-downtime hours scale output by the
  available fraction), so stochastic downtime is modelled as whole-line stop intervals sampled from the empirical
  (minutes, reason) distribution of the line. Station outages (what-if) are extra per-station stop intervals.
* Quality gates: units rejected at a gate leave the line (scrap); reworked units cost extra service time at that gate.
* Output = units that finished the line + units scrapped (MES `actual_qty` counts good + reject). Good = finished.
* Reproducible: independent RNG streams (stops / noise / schedule / quality) per replication seed -> common random numbers
  across scenarios.
No code here (or anywhere in the app) can write to PLC/SCADA: this is a pure in-memory model.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field, replace

import numpy as np
import simpy


@dataclass
class LineParams:
    line_id: str
    stations: list[str]                       # ordered station ids
    gate: list[bool]                          # quality gate at station i
    cycle: list[dict[str, float]]             # per station: product -> standard cycle sec
    buffers_units: list[int]                  # [i] = capacity (units) of the buffer feeding station i ([0] = input buffer)
    products: list[str]
    mix: list[float]                          # product shares (sum 1)
    target_per_hour: float                    # planned release rate (units/h)
    parallelism: float = 1.0                  # units per cycle (calibrated)
    speed: float = 1.0                        # >1 slows (calibrated performance loss)
    cv: float = 0.0                           # cycle-time variability (calibrated)
    lot_units: int = 10
    stop_samples: list[tuple[int, str]] = field(default_factory=lambda: [(0, "NONE")])   # empirical (minutes, reason) per hour
    reject_rate: dict[str, float] = field(default_factory=dict)                           # per product, whole line
    rework_rate: float = 0.0
    setup_min: dict[str, float] = field(default_factory=dict)                            # routing changeover (min) per product


@dataclass
class Outage:
    station_id: str
    start_hour: float
    duration_hours: float
    capacity_frac: float = 0.0                # 0 = fully offline; 0.5 = alternate machine at 50%


@dataclass
class RunConfig:
    horizon_hours: float = 24.0
    demand_pct: float = 0.0
    capacity_pct: float = 0.0
    buffer_factor: float = 1.0
    buffer_override: dict[str, int] = field(default_factory=dict)    # station id (the buffer feeding it) -> units
    cycle_mult: dict[str, float] = field(default_factory=dict)       # station id -> multiplier (0.9 = 10% faster)
    outages: list[Outage] = field(default_factory=list)
    product_schedule: list[str] | None = None                        # product per hour (e.g. actual plan in calibration)
    stops: bool = True
    apply_routing_setup: bool = False                                # NOT supported by data (see MODEL_CARD): what-if only
    cv: float | None = None


def _merge(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


class _Stops:
    """Sorted disjoint stop intervals with O(log n) 'advance by work' and cumulative up-time."""

    def __init__(self, iv):
        self.iv = _merge(iv)
        self.s = [a for a, _ in self.iv]
        self.e = [b for _, b in self.iv]
        self.cum = [0.0]
        for a, b in self.iv:
            self.cum.append(self.cum[-1] + (b - a))

    def advance(self, t: float, work: float) -> float:
        s, e = self.s, self.e
        while True:
            i = bisect_right(e, t)
            if i < len(s):
                if t >= s[i]:
                    t = e[i]
                    continue
                gap = s[i] - t
                if work <= gap:
                    return t + work
                work -= gap
                t = e[i]
            else:
                return t + work

    def up(self, t: float) -> float:
        """Cumulative up-time (seconds not stopped) in [0, t]."""
        i = bisect_right(self.s, t)
        stopped = self.cum[i - 1] if i else 0.0
        if i:
            a, b = self.iv[i - 1]
            stopped += min(max(t - a, 0.0), b - a)
        return t - stopped


def simulate_once(p: LineParams, cfg: RunConfig, seed: int) -> dict:
    T = cfg.horizon_hours * 3600.0
    n = len(p.stations)
    L = p.lot_units
    sid = {s: i for i, s in enumerate(p.stations)}
    rng_stop = np.random.default_rng([seed, 1])
    rng_noise = np.random.default_rng([seed, 2])
    rng_sched = np.random.default_rng([seed, 3])
    rng_q = np.random.default_rng([seed, 4])
    n_hours = int(math.ceil(cfg.horizon_hours))
    sched = (list(cfg.product_schedule)[:n_hours] if cfg.product_schedule
             else [p.products[i] for i in rng_sched.choice(len(p.products), size=n_hours, p=p.mix)])
    while len(sched) < n_hours:                                   # schedule shorter than horizon: repeat last
        sched.append(sched[-1])

    # ---- line stops (whole line)
    line_iv: list[tuple[float, float]] = []
    reasons: dict[str, float] = {}
    if cfg.stops:
        idx = rng_stop.integers(0, len(p.stop_samples), size=n_hours)
        offs = rng_stop.random(n_hours)
        for h in range(n_hours):
            m, why = p.stop_samples[idx[h]]
            if m > 0:
                st = h * 3600.0 + offs[h] * (60 - m) * 60.0
                line_iv.append((st, st + m * 60.0))
                reasons[why] = reasons.get(why, 0.0) + m
    if cfg.apply_routing_setup:
        for h in range(1, n_hours):
            if sched[h] != sched[h - 1]:
                line_iv.append((h * 3600.0, h * 3600.0 + p.setup_min.get(sched[h], 0.0) * 60.0))
    line_stops = _Stops([(a, min(b, T)) for a, b in line_iv if a < T])
    avail = max(line_stops.up(T), 1e-9)

    # ---- station outages / degradation
    st_iv: list[list[tuple[float, float]]] = [list(line_stops.iv) for _ in range(n)]
    degraded: list[list[tuple[float, float, float]]] = [[] for _ in range(n)]
    for o in cfg.outages:
        i = sid[o.station_id]
        a, b = o.start_hour * 3600.0, (o.start_hour + o.duration_hours) * 3600.0
        if o.capacity_frac <= 0:
            st_iv[i].append((a, b))
        else:
            degraded[i].append((a, b, 1.0 / o.capacity_frac))
    stops = [_Stops(iv) if len(iv) != len(line_stops.iv) else line_stops for iv in st_iv]

    # ---- buffers (in lots)
    caps = []
    for i, st in enumerate(p.stations):
        units = cfg.buffer_override.get(st, p.buffers_units[i] * cfg.buffer_factor)
        caps.append(max(1, int(math.ceil(units / L))))
    env = simpy.Environment()
    store = [simpy.Store(env, capacity=c) for c in caps]

    cv = p.cv if cfg.cv is None else cfg.cv
    sig = math.sqrt(math.log(1 + cv * cv)) if cv > 0 else 0.0
    mu = -sig * sig / 2
    base_mult = p.speed / (p.parallelism * (1 + cfg.capacity_pct / 100.0))
    n_gates = max(1, sum(p.gate))
    rw_gate = p.rework_rate / n_gates

    busy = [0.0] * n
    blocked = [0.0] * n
    starved = [0.0] * n
    wait_sum = [0.0] * n
    wait_cnt = [0] * n
    counters = {"finished": 0, "scrapped": 0, "released": 0, "wip": 0, "area": 0.0, "last": 0.0}
    hourly = [0.0] * n_hours

    def wip_change(d):
        counters["area"] += counters["wip"] * (env.now - counters["last"])
        counters["last"] = env.now
        counters["wip"] += d

    def hour(t):
        return min(int(t // 3600), n_hours - 1)

    def station(i):
        inb = store[i]
        outb = store[i + 1] if i < n - 1 else None
        stp = stops[i]
        cyc = p.cycle[i]
        mult_i = cfg.cycle_mult.get(p.stations[i], 1.0)
        gate = p.gate[i]
        while True:
            t0 = env.now
            lot = yield inb.get()
            starved[i] += stp_up(stp, env.now) - stp_up(stp, t0)
            wait_sum[i] += env.now - lot[2]
            wait_cnt[i] += 1
            size, prod = lot[0], lot[1]
            per_unit = cyc[prod] * mult_i * base_mult
            for a, b, m in degraded[i]:
                if a <= env.now < b:
                    per_unit *= m
            work = size * per_unit * (rng_noise.lognormal(mu, sig) if sig else 1.0)
            if gate:
                if rw_gate > 0:
                    work += rng_q.binomial(size, min(1.0, rw_gate)) * per_unit
            start = env.now
            fin = stp.advance(start, work)
            yield env.timeout(fin - start)
            busy[i] += work
            if gate:
                r = p.reject_rate.get(prod, 0.0) / n_gates
                if r > 0:
                    k = int(rng_q.binomial(size, r))
                    if k:
                        size -= k
                        lot[0] = size
                        counters["scrapped"] += k
                        hourly[hour(env.now)] += k
                        wip_change(-k)
            t1 = env.now
            if outb is None:
                counters["finished"] += size
                hourly[hour(env.now)] += size
                wip_change(-size)
            else:
                lot[2] = env.now
                yield outb.put(lot)
                blocked[i] += stp_up(stp, env.now) - stp_up(stp, t1)
                lot[2] = env.now

    def stp_up(stp, t):
        return stp.up(t)

    rate = p.target_per_hour / 3600.0 * (1 + cfg.demand_pct / 100.0)
    interval = L / rate if rate > 0 else float("inf")
    demand_total = rate * T

    def release():
        while True:
            yield env.timeout(interval)
            prod = sched[hour(env.now)]
            lot = [L, prod, env.now]
            yield store[0].put(lot)
            counters["released"] += L
            wip_change(+L)

    q_samples = [[] for _ in range(n)]
    wip_hourly: list[float] = []

    def monitor():
        k = 0
        while True:
            yield env.timeout(120)
            k += 1
            for i in range(n):
                q_samples[i].append(len(store[i].items) * L)
            if k % 30 == 0:
                wip_hourly.append(counters["wip"])

    for i in range(n):
        env.process(station(i))
    if rate > 0:
        env.process(release())
    env.process(monitor())
    env.run(until=T)
    wip_change(0)

    out_units = counters["finished"] + counters["scrapped"]
    util = [busy[i] / avail * 100 for i in range(n)]
    return {
        "output_units": out_units, "good_units": counters["finished"], "scrapped_units": counters["scrapped"],
        "released_units": counters["released"], "wip_end_units": counters["wip"],
        "conservation_gap": counters["released"] - out_units - counters["wip"],
        "wip_avg_units": counters["area"] / T, "backlog_units": max(0.0, demand_total - counters["released"]),
        "util_pct": util, "blocked_pct": [b / avail * 100 for b in blocked], "starved_pct": [s / avail * 100 for s in starved],
        "avg_queue_units": [float(np.mean(q)) if q else 0.0 for q in q_samples],
        "avg_wait_sec": [wait_sum[i] / wait_cnt[i] if wait_cnt[i] else 0.0 for i in range(n)],
        "hourly_output": hourly, "wip_hourly": wip_hourly, "line_down_min": (T - avail) / 60.0, "downtime_by_reason": reasons,
        "bottleneck_idx": int(np.argmax(util)),
    }


_POOL = None


def _run_task(args):
    p, cfg, seed = args
    return simulate_once(p, cfg, seed)


def run_many(tasks: list[tuple[LineParams, RunConfig, int]]) -> list[dict]:
    """Run independent simulations, in parallel on a persistent process pool when worthwhile.
    TWIN_WORKERS=0 (or any pool failure) falls back to serial execution; results are identical either way (seeded)."""
    import os
    global _POOL
    workers = int(os.environ.get("TWIN_WORKERS", min(6, os.cpu_count() or 1)))
    if workers <= 1 or len(tasks) < 6:
        return [_run_task(t) for t in tasks]
    try:
        from concurrent.futures import ProcessPoolExecutor
        if _POOL is None:
            _POOL = ProcessPoolExecutor(max_workers=workers)
        return list(_POOL.map(_run_task, tasks, chunksize=max(1, len(tasks) // (workers * 3))))
    except Exception:  # BrokenProcessPool, spawn problems, etc.
        _POOL = None
        return [_run_task(t) for t in tasks]


def _q(a, lo, hi):
    return float(np.percentile(a, lo)), float(np.percentile(a, hi))


def replicate(p: LineParams, cfg: RunConfig, reps: int = 30, seed: int = 1234) -> dict:
    """Run `reps` seeded replications; return means and 90% intervals (5th-95th percentile across replications)."""
    runs = run_many([(p, cfg, seed + r) for r in range(reps)])
    n = len(p.stations)
    arr = lambda k: np.array([r[k] for r in runs], dtype=float)
    out = arr("output_units")
    util = np.array([r["util_pct"] for r in runs])
    bn = np.array([r["bottleneck_idx"] for r in runs])
    t_crit = 1.645 if reps >= 30 else {1: 6.314, 2: 2.92, 3: 2.353, 4: 2.132, 5: 2.015}.get(reps - 1, 1.8)
    se = float(out.std(ddof=1) / math.sqrt(reps)) if reps > 1 else 0.0
    mean_util = util.mean(axis=0)
    line_util = float(mean_util.max())
    stations = []
    for i, s in enumerate(p.stations):
        stations.append({
            "station_id": s, "utilization_pct": float(mean_util[i]),
            "avg_queue_units": float(np.mean([r["avg_queue_units"][i] for r in runs])),
            "avg_wait_sec": float(np.mean([r["avg_wait_sec"][i] for r in runs])),
            "blocked_pct": float(np.mean([r["blocked_pct"][i] for r in runs])),
            "starved_pct": float(np.mean([r["starved_pct"][i] for r in runs])),
            "bottleneck_frequency": float((bn == i).mean()),
        })
    # bottleneck = most utilised station on average; ties broken by most blocking of upstream station
    order = sorted(range(n), key=lambda i: (-round(mean_util[i], 1), -(stations[i - 1]["blocked_pct"] if i else 0)))
    bi = order[0]
    for i, st in enumerate(stations):
        st["is_bottleneck"] = i == bi
    hourly = np.array([r["hourly_output"] for r in runs])
    return {
        "reps": reps, "seed": seed, "horizon_hours": cfg.horizon_hours,
        "output": {"mean": float(out.mean()), "p05": _q(out, 5, 95)[0], "p95": _q(out, 5, 95)[1], "std": float(out.std(ddof=1)) if reps > 1 else 0.0,
                   "ci90_halfwidth_of_mean": t_crit * se},
        "good_units": float(arr("good_units").mean()), "scrapped_units": float(arr("scrapped_units").mean()),
        "wip": {"mean": float(arr("wip_avg_units").mean()), "p05": _q(arr("wip_avg_units"), 5, 95)[0], "p95": _q(arr("wip_avg_units"), 5, 95)[1]},
        "backlog_units": float(arr("backlog_units").mean()),
        "utilization_pct": line_util, "stations": stations, "bottleneck_station_id": p.stations[bi],
        "line_down_min": float(arr("line_down_min").mean()),
        "max_conservation_gap": float(np.abs(arr("conservation_gap")).max()),
        "hourly_mean": hourly.mean(axis=0).tolist(),
        "wip_hourly_mean": np.array([(r["wip_hourly"] + [r["wip_end_units"]] * 200)[:int(cfg.horizon_hours)] for r in runs]).mean(axis=0).tolist(),
        "hourly_p05": np.percentile(hourly, 5, axis=0).tolist(), "hourly_p95": np.percentile(hourly, 95, axis=0).tolist(),
        "wait_sec_line": float(sum(s["avg_wait_sec"] for s in stations)),
    }
