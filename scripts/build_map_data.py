#!/usr/bin/env python3
"""
Downloads the official CCP SDE JSONL and produces optimized static JSON files
for the Vigilant star-map frontend.

Output (written to frontend/public/data/):
  systems.json  — ~5,200 K-space systems with normalized 2-D coordinates
  edges.json    — ~6,900 deduplicated stargate pairs
  regions.json  — region centroids for label placement

The generator itself lives in app/sde/map_data.py (the app runs it after
every SDE import); this script is the download + command-line wrapper.

Usage:
  python scripts/build_map_data.py              # download + generate
  python scripts/build_map_data.py --cache      # reuse previously downloaded ZIP
"""
import argparse
import io
import logging
import sys
import zipfile
from pathlib import Path

import urllib.request

# scripts/ has no __init__.py, so running this file puts scripts/ (not the
# repo root) first on sys.path; add the root so `app` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sde.map_data import build_from_zip  # noqa: E402

log = logging.getLogger(__name__)

SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
CACHE_PATH = Path("scripts/.sde_cache.zip")
# CLI default: write to the frontend's Vite static input dir (build-time seed).
# Runtime callers (app/sde/loader.py post-import) pass `/data/map/` instead so
# the running app can refresh K-space JSONs without a redeploy. See ISS-019.
DEFAULT_OUTPUT_DIR = Path("frontend/public/data")


def download_sde(use_cache: bool) -> zipfile.ZipFile:
    if use_cache and CACHE_PATH.exists():
        log.info("Using cached SDE ZIP at %s", CACHE_PATH)
        return zipfile.ZipFile(CACHE_PATH)

    log.info("Downloading SDE from %s ...", SDE_URL)
    req = urllib.request.Request(SDE_URL, headers={"User-Agent": "Vigilant-MapBuilder/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    log.info("Downloaded %s bytes", f"{len(data):,}")

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_bytes(data)
    return zipfile.ZipFile(io.BytesIO(data))


def build(use_cache: bool, output_dir: Path = DEFAULT_OUTPUT_DIR):
    zf = download_sde(use_cache)
    build_from_zip(zf, output_dir)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Build star-map JSON data from EVE SDE")
    parser.add_argument("--cache", action="store_true", help="Reuse previously downloaded SDE ZIP")
    args = parser.parse_args()
    try:
        build(use_cache=args.cache)
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)
