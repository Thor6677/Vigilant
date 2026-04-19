"""Capacitor discrete-event simulator for EVE Online fitting.

Simulates module activations over time using a priority queue.  Each module
activation is an event that drains (or injects) capacitor.  Between events
the cap regenerates using EVE's non-linear recharge curve.

Cap recharge formula (EVE):
    cap(t) = C_max * (1 + (sqrt(cap_prev / C_max) - 1) * exp((t_prev - t) / tau))^2
    where tau = recharge_rate_ms / 5000.0  (recharge_rate is in ms, tau in seconds)

Peak recharge occurs at 25% cap: rate = 2.5 * C_max / (recharge_rate_ms / 1000)

Stability detection: compute the LCM of all module cycle times.  At each
period boundary, compare cap to the previous period.  If cap >= previous
cap, the fit is stable and simulation ends early.

Algorithm reference: Pyfa eos/capSim.py (discrete event sim with min-heap)
"""

import heapq
import math
from dataclasses import dataclass, field


# Maximum simulation time: 6 hours in milliseconds
MAX_SIM_TIME_MS = 6 * 3600 * 1000

# Minimum meaningful cap drain (ignore sub-0.01 GJ/s modules)
MIN_CAP_DRAIN = 0.01


@dataclass(order=True)
class CapEvent:
    """A scheduled module activation in the cap simulation."""
    time_ms: float
    # Fields below are not used for ordering
    cap_need: float = field(compare=False)
    cycle_ms: float = field(compare=False)
    count: int = field(compare=False)       # number of identical grouped modules
    label: str = field(compare=False, default="")


def _cap_at_time(
    cap_prev: float, cap_max: float, tau: float,
    t_prev_ms: float, t_now_ms: float
) -> float:
    """Compute cap level at t_now given cap_prev at t_prev, using EVE's regen curve."""
    if cap_max <= 0 or tau <= 0:
        return cap_prev
    dt_s = (t_now_ms - t_prev_ms) / 1000.0
    if dt_s <= 0:
        return cap_prev
    sqrt_frac = math.sqrt(cap_prev / cap_max) if cap_prev > 0 else 0.0
    regen = cap_max * (1.0 + (sqrt_frac - 1.0) * math.exp(-dt_s / tau)) ** 2
    return min(regen, cap_max)


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def _lcm(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return abs(a * b) // _gcd(a, b)


def simulate_cap(
    cap_max: float,
    recharge_rate_ms: float,
    modules: list[dict],
) -> dict:
    """Run the capacitor simulation.

    Args:
        cap_max: Maximum capacitor capacity (GJ).
        recharge_rate_ms: Cap recharge time in milliseconds.
        modules: List of dicts with keys:
            cap_need (float): GJ per activation (positive = drain, negative = inject)
            duration_ms (float): Cycle time in milliseconds
            count (int): Number of identical modules (grouped)
            label (str): Optional label for debugging

    Returns dict with:
        stable (bool): Whether the fit is cap-stable.
        stable_pct (float): Equilibrium cap percentage (0-100). 0 if unstable.
        lowest_pct (float): Lowest cap percentage observed during simulation.
        time_to_empty_s (float): Seconds until cap hits 0 (0 if stable).
        peak_recharge (float): Peak recharge rate in GJ/s.
        total_drain (float): Total average drain rate in GJ/s.
    """
    if cap_max <= 0 or recharge_rate_ms <= 0:
        return _empty_result()

    tau = recharge_rate_ms / 5000.0  # tau in seconds
    peak_recharge = 2.5 * cap_max / (recharge_rate_ms / 1000.0)

    # Filter to modules that actually use cap
    active_mods = [
        m for m in modules
        if m.get("duration_ms", 0) > 0 and abs(m.get("cap_need", 0)) > MIN_CAP_DRAIN
    ]

    if not active_mods:
        return {
            "stable": True,
            "stable_pct": 100.0,
            "lowest_pct": 100.0,
            "time_to_empty_s": 0.0,
            "peak_recharge": round(peak_recharge, 1),
            "total_drain": 0.0,
        }

    # Compute total average drain for the stats output
    total_drain = 0.0
    for m in active_mods:
        cap = m["cap_need"]
        dur = m["duration_ms"]
        cnt = m.get("count", 1)
        if cap > 0 and dur > 0:
            total_drain += (cap / (dur / 1000.0)) * cnt

    # Build the event heap.  Stagger identical modules across their cycle time
    # so they don't all fire at t=0 (more realistic, matches Pyfa behavior).
    # Exception: turrets are NOT staggered (they fire together in EVE).
    heap: list[CapEvent] = []
    for m in active_mods:
        cap_need = m["cap_need"]
        cycle_ms = m["duration_ms"]
        count = m.get("count", 1)
        stagger = m.get("stagger", True)
        label = m.get("label", "")

        if count <= 1 or not stagger:
            # Single module or no stagger — all fire at t=0
            heapq.heappush(heap, CapEvent(
                time_ms=0.0,
                cap_need=cap_need * count,
                cycle_ms=cycle_ms,
                count=count,
                label=label,
            ))
        else:
            # Stagger: spread first activations evenly across the cycle
            offset = cycle_ms / count
            for i in range(count):
                heapq.heappush(heap, CapEvent(
                    time_ms=offset * i,
                    cap_need=cap_need,
                    cycle_ms=cycle_ms,
                    count=1,
                    label=f"{label}#{i}",
                ))

    # Compute the period for stability detection (LCM of all cycle times,
    # rounded to integer ms to avoid floating-point drift).
    period_ms = 1
    for m in active_mods:
        cycle_int = max(1, round(m["duration_ms"]))
        period_ms = _lcm(period_ms, cycle_int)
    # Cap period at a reasonable value to avoid huge LCMs
    if period_ms > MAX_SIM_TIME_MS:
        period_ms = MAX_SIM_TIME_MS

    # Run the simulation
    cap = cap_max
    t_last = 0.0
    lowest_cap = cap_max
    cap_at_last_period = -1.0
    last_period_boundary = 0.0

    iterations = 0
    max_iterations = 500_000  # safety limit

    while heap and iterations < max_iterations:
        iterations += 1
        event = heapq.heappop(heap)

        if event.time_ms > MAX_SIM_TIME_MS:
            # Ran for 6 hours without going empty — stable
            break

        # Regenerate cap from t_last to event.time_ms
        if event.time_ms > t_last:
            cap = _cap_at_time(cap, cap_max, tau, t_last, event.time_ms)
            t_last = event.time_ms

        # Apply the activation drain/injection
        cap -= event.cap_need
        cap = min(cap, cap_max)  # don't exceed max (for injections)

        if cap < 0:
            # Cap depleted — unstable
            time_to_empty_s = event.time_ms / 1000.0
            return {
                "stable": False,
                "stable_pct": 0.0,
                "lowest_pct": round(max(0.0, lowest_cap / cap_max * 100), 1),
                "time_to_empty_s": round(time_to_empty_s),
                "peak_recharge": round(peak_recharge, 1),
                "total_drain": round(total_drain, 1),
            }

        lowest_cap = min(lowest_cap, cap)

        # Period-based stability detection
        if event.time_ms >= last_period_boundary + period_ms:
            if cap_at_last_period >= 0 and cap >= cap_at_last_period - 0.01:
                # Cap is the same or higher than last period — stable
                break
            cap_at_last_period = cap
            last_period_boundary = event.time_ms

        # Schedule next activation
        next_time = event.time_ms + event.cycle_ms
        heapq.heappush(heap, CapEvent(
            time_ms=next_time,
            cap_need=event.cap_need,
            cycle_ms=event.cycle_ms,
            count=event.count,
            label=event.label,
        ))

    # If we got here, the fit is stable
    stable_pct = lowest_cap / cap_max * 100 if cap_max > 0 else 100.0

    # Compute EVE's displayed stable percentage using the analytical formula:
    # p = 0.25 * (1 + sqrt(max(0, -(2*avgDrain*tau - capMax) / capMax)))^2
    # where avgDrain is in GJ/s and tau is in seconds
    if total_drain > 0 and tau > 0:
        inner = -(2.0 * total_drain * tau - cap_max) / cap_max
        if inner >= 0:
            eve_pct = 0.25 * (1.0 + math.sqrt(inner)) ** 2 * 100.0
        else:
            eve_pct = 0.0
        # Use the analytical formula for the displayed value (matches EVE client)
        stable_pct = eve_pct

    return {
        "stable": True,
        "stable_pct": round(min(stable_pct, 100.0), 1),
        "lowest_pct": round(lowest_cap / cap_max * 100, 1) if cap_max > 0 else 100.0,
        "time_to_empty_s": 0.0,
        "peak_recharge": round(peak_recharge, 1),
        "total_drain": round(total_drain, 1),
    }


def _empty_result() -> dict:
    return {
        "stable": True,
        "stable_pct": 100.0,
        "lowest_pct": 100.0,
        "time_to_empty_s": 0.0,
        "peak_recharge": 0.0,
        "total_drain": 0.0,
    }
