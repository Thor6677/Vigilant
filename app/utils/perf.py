import logging
import os
import time

logger = logging.getLogger("vigilant.perf")

_ENABLED = os.environ.get("VIGILANT_PERF_LOG", "").lower() in ("1", "true", "yes")


def perf_enabled() -> bool:
    return _ENABLED


def now() -> float:
    return time.perf_counter()


def ms_since(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def perf_log(route: str, total_ms: float, **section_ms: float) -> None:
    if not _ENABLED:
        return
    parts = [f"perf route={route}", f"total_ms={total_ms:.0f}"]
    for k, v in section_ms.items():
        if v is None:
            continue
        parts.append(f"{k}_ms={v:.0f}")
    logger.info(" ".join(parts))
