"""ISS-172: the VIGILANT_TAG pin rewrites .env through .env.tmp. That temp file
holds every secret, so it must be owner-only from creation, not after the
write. chmod is shimmed to a no-op here so only the creation mode is judged.
Only the pin block is run (the scripts need Docker and a live host)."""
import os
import re
import stat
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(REPO, "scripts", name)) as fh:
        return fh.read()


def _deploy_block():
    m = re.search(r"^_pin_tag_in_env\(\) \{\n.*?^\}\n", _src("deploy.sh"), re.M | re.S)
    assert m, "deploy.sh no longer defines _pin_tag_in_env"
    return m.group(0) + '\n_pin_tag_in_env "v9"\n'


def _rollback_block():
    m = re.search(r"^touch \.env\n.*?^mv \.env\.tmp \.env\n", _src("rollback.sh"), re.M | re.S)
    assert m, "rollback.sh no longer pins the tag into .env"
    return 'TARGET="v9"\n' + m.group(0)


def _run(block, tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "chmod").write_text("#!/bin/sh\nexit 0\n")
    (shim / "chmod").chmod(0o755)
    (tmp_path / ".env").write_text("SECRET_KEY=s\nVIGILANT_TAG=old\n")
    # A leftover temp file with loose permissions must not be reused.
    (tmp_path / ".env.tmp").write_text("stale\n")
    (tmp_path / ".env.tmp").chmod(0o644)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}
    subprocess.run(["bash", "-euc", "umask 022\n" + block], cwd=tmp_path, env=env, check=True)
    out = tmp_path / ".env"
    assert out.read_text() == "SECRET_KEY=s\nVIGILANT_TAG=v9\n"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_deploy_pin_creates_the_temp_file_owner_only(tmp_path):
    _run(_deploy_block(), tmp_path)


def test_rollback_pin_creates_the_temp_file_owner_only(tmp_path):
    _run(_rollback_block(), tmp_path)
