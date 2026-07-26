import pytest

from app.industry.invention import (
    attempt_cost, invented_bpc, invention_overhead_per_unit,
    invention_probability,
)


def test_probability_reference_dcii_all_iv_no_decryptor():
    p = invention_probability(0.34, 4, 4, 4)
    assert p == pytest.approx(0.34 * (1 + 4/40 + (4+4)/30), abs=1e-4)  # 0.46466...


def test_probability_clamps():
    assert invention_probability(0.34, 9, 9, 9) <= 1.0          # level clamp → 5s
    assert invention_probability(0.34, 5, 5, 5) == pytest.approx(
        0.34 * (1 + 5/40 + 10/30))
    assert invention_probability(0.0, 4, 4, 4) == 0.0
    assert invention_probability(2.0, 5, 5, 5) == 1.0            # P clamp


def test_attempt_cost_unpriced_datacore_is_none():
    dcs = [{"material_type_id": 20410, "quantity": 8},
           {"material_type_id": 20424, "quantity": 8}]
    assert attempt_cost(dcs, {20410: 100.0}) is None
    assert attempt_cost(dcs, {20410: 100.0, 20424: 50.0},
                        decryptor_price=1000.0) == pytest.approx(
        8*100 + 8*50 + 1000)


def test_invented_bpc_mods_and_floors():
    class D:  # minimal decryptor stub
        run_mod, me_mod = +2, -1
    assert invented_bpc(1, None) == (1, 2)
    assert invented_bpc(1, D) == (3, 1)
    class Harsh:
        run_mod, me_mod = -9, -9
    assert invented_bpc(1, Harsh) == (1, 0)   # floors


def test_overhead_full_chain():
    # attempt 1300 ISK, P=0.4647, 1 run/success, 1 unit/run
    o = invention_overhead_per_unit(1300.0, 0.46466, 1, 1)
    assert o == pytest.approx(1300.0 / 0.46466)
    assert invention_overhead_per_unit(1300.0, 0.0, 1, 1) is None
    assert invention_overhead_per_unit(1300.0, 0.5, 0, 1) is None
