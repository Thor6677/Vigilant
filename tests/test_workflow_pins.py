"""Third-party GitHub Actions are pinned to a full commit SHA.

A tag like `@v2` can be moved to different code by whoever controls that
repository, and the release job runs with write access to this repository and
its package registry. GitHub's own `actions/*` stay on version tags.
"""
import glob
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)(?:\s*#\s*(\S+))?", re.MULTILINE)


def _uses():
    for path in sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml"))):
        for m in USES.finditer(open(path).read()):
            yield os.path.basename(path), m.group(1), m.group(2)


def test_workflows_are_found():
    assert any(True for _ in _uses())


def test_third_party_actions_are_pinned_to_a_commit_with_its_version_noted():
    loose = []
    for wf, ref, comment in _uses():
        action, _, version = ref.partition("@")
        if action.startswith("actions/") or action.startswith("./"):
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", version) or not (comment or "").startswith("v"):
            loose.append(f"{wf}: {ref}")
    assert not loose, "pin these to a commit SHA with a '# vX.Y.Z' comment:\n  " + "\n  ".join(loose)
