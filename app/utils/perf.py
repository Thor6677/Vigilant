import logging
import logging.handlers
import os
import pathlib
import time

logger = logging.getLogger("vigilant.perf")

_ENABLED = os.environ.get("VIGILANT_PERF_LOG", "").lower() in ("1", "true", "yes")
_PERF_FILE = os.environ.get("VIGILANT_PERF_FILE", "").strip()


def _install_file_handler() -> None:
    """Mirror vigilant.perf to a rotating file on the persistent /data
    volume so perf measurements survive container recreates (ISS-013).
    No-op if VIGILANT_PERF_FILE is empty or the path can't be opened —
    stdout remains the canonical sink either way."""
    if not _PERF_FILE:
        return
    try:
        pathlib.Path(_PERF_FILE).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            _PERF_FILE,
            maxBytes=5 * 1024 * 1024,  # 5 MiB per file
            backupCount=4,             # → ~25 MiB total ceiling
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        # Avoid double-installing if the module re-imports during dev reloads.
        for h in logger.handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == handler.baseFilename:
                return
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # Don't suppress propagation — the root logger still emits to stdout
        # for `docker logs` visibility; this handler is additive.
    except OSError:
        # Read-only fs, permission denied, etc. Log to stdout-only and move on.
        pass


if _ENABLED:
    _install_file_handler()


def perf_enabled() -> bool:
    return _ENABLED


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
