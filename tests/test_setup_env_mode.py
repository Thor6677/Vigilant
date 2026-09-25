""".env holds SECRET_KEY and the EVE client secret, so the scripts that touch
it leave it owner-only. setup_vps.sh runs as root on a fresh host and cannot run
here, so this runs its .env block alone against a temp directory."""
import os
import re
import stat
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_block() -> str:
    src = open(os.path.join(REPO, "setup_vps.sh")).read()
    m = re.search(r"^if \[ -f /opt/vigilant/\.env \]; then\n.*?^fi\n", src, re.MULTILINE | re.DOTALL)
    assert m, "setup_vps.sh no longer tightens an existing .env"
    return m.group(0)


def test_setup_tightens_an_existing_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SECRET_KEY=x\n")
    env.chmod(0o644)
    block = _env_block().replace("/opt/vigilant", str(tmp_path))
    subprocess.run(["bash", "-euc", block], check=True)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_setup_is_fine_without_an_env(tmp_path):
    block = _env_block().replace("/opt/vigilant", str(tmp_path))
    subprocess.run(["bash", "-euc", block], check=True)
    assert not (tmp_path / ".env").exists()


def test_the_printed_copy_step_creates_it_owner_only():
    src = open(os.path.join(REPO, "setup_vps.sh")).read()
    step = next(line for line in src.splitlines() if ".env.example" in line)
    assert "install -m 600" in step
