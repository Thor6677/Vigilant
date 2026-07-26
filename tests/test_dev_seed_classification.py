"""Every model must have a declared dev-seed behaviour.

This is the regression guard that keeps the seeder from rotting. Without it, the
first feature that adds a model produces a dev database silently missing a
table, and the failure shows up as a confusing 500 in dev weeks later.

Loaded by path rather than imported as a package: scripts/ is not a package, and
scripts/dev_seed_tables.py is deliberately import-free (pure data) so the seeder
can run inside a bare container with no app dependencies.
"""
import importlib.util
import pathlib

import app.db.sde_models  # noqa: F401 — registers sde_* tables on Base
from app.db.models import Base

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "dev_seed_tables.py"
_spec = importlib.util.spec_from_file_location("dev_seed_tables", _PATH)
dev_seed_tables = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dev_seed_tables)


def test_classification_sets_are_disjoint():
    a, b, c = (set(dev_seed_tables.COPY_ALL),
               set(dev_seed_tables.SLICE),
               set(dev_seed_tables.SKIP))
    assert not (a & b), f"table in both COPY_ALL and SLICE: {a & b}"
    assert not (a & c), f"table in both COPY_ALL and SKIP: {a & c}"
    assert not (b & c), f"table in both SLICE and SKIP: {b & c}"


def test_every_model_table_is_classified():
    classified = (set(dev_seed_tables.COPY_ALL)
                  | set(dev_seed_tables.SLICE)
                  | set(dev_seed_tables.SKIP))
    missing = set(Base.metadata.tables) - classified
    assert not missing, (
        "New model(s) added without a dev-seed decision: "
        f"{sorted(missing)}. Add each to COPY_ALL, SLICE, or SKIP in "
        "scripts/dev_seed_tables.py."
    )


def test_no_phantom_tables_in_classification():
    classified = (set(dev_seed_tables.COPY_ALL)
                  | set(dev_seed_tables.SLICE)
                  | set(dev_seed_tables.SKIP))
    phantom = classified - set(Base.metadata.tables)
    assert not phantom, f"Classified tables that no model defines: {sorted(phantom)}"
