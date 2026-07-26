"""Semantic-version comparison for the in-app update checker.

Deliberately hand-rolled rather than adding a `packaging` dependency: the only
inputs are our own release tags, the rules needed are a small subset of semver,
and the fail-closed behaviour below is easier to guarantee in a few lines than
to configure in a library.
"""
import re

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def parse_version(raw: str | None) -> tuple[int, int, int, str] | None:
    """Parse ``v1.2.3`` / ``1.2.3`` / ``v1.2.3-rc1`` into a comparable tuple.

    Returns None for anything that is not a semantic version — notably the
    "dev" sentinel that source builds report.
    """
    if not raw:
        return None
    m = _SEMVER.match(raw.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def is_newer(latest: str | None, running: str | None) -> bool:
    """Whether ``latest`` is a strictly newer release than ``running``.

    Fails closed: if either side is unparseable — which includes the "dev"
    version a source build reports — this returns False and no notification is
    ever sent. That is the whole mechanism keeping the dev instance quiet, and
    it is why no separate "is this dev?" flag exists.
    """
    a, b = parse_version(latest), parse_version(running)
    if a is None or b is None:
        return False
    if a[:3] != b[:3]:
        return a[:3] > b[:3]
    # Same x.y.z: an absent prerelease outranks any prerelease, so
    # v1.3.0 > v1.3.0-rc1, and two prereleases compare lexicographically.
    if a[3] == b[3]:
        return False
    if not a[3]:
        return True
    if not b[3]:
        return False
    return a[3] > b[3]
