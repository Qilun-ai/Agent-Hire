"""Monotonic elapsed-time measurement with explicit unknown legacy durations."""
from datetime import datetime
import math
import time


def elapsed(start):
    return max(0.0, time.perf_counter() - start)


def recorded_duration(report):
    value = report.get("duration_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value), "monotonic"
    # Earlier source reports recorded start/end UTC timestamps, but no phase timers.
    try:
        start = datetime.fromisoformat(report["started_at"])
        end = datetime.fromisoformat(report["finished_at"])
        value = (end - start).total_seconds()
        return (value, "legacy_timestamp_estimate") if value >= 0 else (None, "not_recorded")
    except (KeyError, TypeError, ValueError):
        return None, "not_recorded"


def format_duration(seconds):
    if seconds is None:
        return "not recorded"
    if seconds < 60:
        return f"{seconds:.3f}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {seconds:.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes}m {seconds:.1f}s"


def report_timing(report):
    """Expose historical source estimates without inventing a company runtime."""
    seconds, basis = recorded_duration(report or {})
    sources = []
    for source in (report or {}).get("sources", []):
        source_seconds, source_basis = recorded_duration(source)
        sources.append({"adapter": source["adapter"], "source_url": source["source_url"],
                        "duration_seconds": source_seconds, "basis": source_basis,
                        "reused_from_completed_snapshot": source.get("reused_from_completed_snapshot", False),
                        "timings": source.get("timings", {})})
    return {"duration_seconds": seconds, "basis": basis, "sources": sources}
