"""The dev instance must stay off the shared edge network and off prod's tags.

Two hazards were verified by hand on 2026-07-26 and are locked in here:

1. A compose OVERLAY CANNOT REMOVE A NETWORK. `docker compose -f base -f
   overlay config` with `networks: []` in the overlay still renders
   `networks: {web: null}` — the service joins `web` anyway. Edge nginx
   resolves upstreams by container name at request time, so a dev container on
   that bridge is one typo away from serving public traffic. Hence dev is a
   STANDALONE compose file, not an overlay. If someone later "simplifies" it
   into an overlay, this test fails.

2. Dev and prod share one Docker daemon. If dev's service inherited prod's
   `image: ghcr.io/thor6677/vigilant:${VIGILANT_TAG:-latest}` while adding
   `build:`, a dev build would OVERWRITE the locally cached prod image tag,
   and a later prod `up` without a pull would run dev code.

Parsed as YAML rather than shelled out to `docker compose config`, so the suite
stays hermetic and runs in CI without a Docker daemon.
"""
import yaml

DEV = "docker-compose.dev.yml"


def _dev():
    with open(DEV) as fh:
        return yaml.safe_load(fh)


def _app(cfg):
    return cfg["services"]["app"]


def test_dev_is_standalone_not_an_overlay():
    cfg = _dev()
    # A standalone file defines the full service, not a fragment.
    assert "image" in _app(cfg)
    assert "volumes" in _app(cfg)


def test_dev_declares_no_web_network():
    cfg = _dev()
    assert "web" not in (cfg.get("networks") or {}), \
        "dev must not declare the shared edge network"
    assert "web" not in (_app(cfg).get("networks") or []), \
        "dev service must not join `web`"


def test_dev_pins_its_own_image_tag():
    assert _app(_dev())["image"] == "vigilant-dev:local"


def test_dev_publishes_only_to_loopback():
    for p in _app(_dev())["ports"]:
        assert str(p).startswith("127.0.0.1:"), \
            f"dev port {p!r} must bind loopback only"


def test_dev_keeps_prod_hardening_and_smaller_limits():
    app = _app(_dev())
    assert app["read_only"] is True
    assert app["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in app["security_opt"]
    limits = app["deploy"]["resources"]["limits"]
    assert limits["memory"] == "1024M"
    assert float(limits["cpus"]) < 2.0
