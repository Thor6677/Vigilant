from pydantic import Field
from pydantic_settings import BaseSettings
from functools import lru_cache


# Contact embedded in every outbound HTTP User-Agent. ESI, zKillboard,
# EVE-Offline and EVE-Scout all REQUIRE a reachable contact so their operators
# can reach whoever is generating the traffic — so this must never be blank.
# The fallback is the project's issue tracker, which reaches the maintainers but
# NOT the operator of any given instance. Self-hosters should set CONTACT_EMAIL
# to their own address so remote operators can reach them directly.
DEFAULT_CONTACT = "https://github.com/Thor6677/Vigilant"

# Product name shared by every outbound User-Agent. The version half comes from
# Settings.version (baked into the image at build time), so remote API operators
# can tell which build is generating traffic. See user_agent() below.
UA_PRODUCT_NAME = "Vigilant"


class Settings(BaseSettings):
    eve_client_id: str
    eve_client_secret: str
    eve_callback_url: str = "http://localhost:8000/auth/callback"
    secret_key: str
    database_url: str = "sqlite+aiosqlite:///./vigilant.db"
    uploads_dir: str = "./uploads"
    debug: bool = False

    # Build version, injected by CI as a Docker build-arg and surfaced as the
    # VIGILANT_VERSION env var (see Dockerfile). Source builds leave it at
    # "dev", which the update checker treats as "never notify" — that is what
    # keeps a dev instance silent without needing its own flag.
    # validation_alias: pydantic-settings would otherwise look for `VERSION`,
    # far too generic a name to claim in a container's environment. Note that
    # because of the alias, `Settings(version=...)` is silently ignored — set
    # the env var instead (tests/test_version_identity.py documents this).
    version: str = Field(default="dev", validation_alias="VIGILANT_VERSION")

    # Master switch for every recurring/ingest background task started in
    # app.main.startup(). True everywhere in production; a dev instance sets it
    # False so it renders pages against seeded data without re-doing
    # production's ESI/zKB polling or competing for the shared disk. Per-job
    # opt-in on top of this comes from the existing killmail_*/everef_* flags,
    # so this stays one switch rather than growing a flag per task.
    background_jobs_enabled: bool = True

    # In-app update checker: polls the GitHub Releases API for this repo and
    # announces a newer release once, via Discord and an admin banner. It NEVER
    # auto-updates — the operator always chooses when to deploy. A build
    # reporting version "dev" never notifies, so a dev instance is silent
    # without needing a flag of its own.
    update_check_enabled: bool = True
    update_check_repo: str = "Thor6677/Vigilant"

    # EVE character id of the owner. On a fresh deploy with no admin yet, ONLY
    # the account that owns this character is auto-promoted to admin at startup.
    # Unset (None) means no one is auto-promoted (fail closed) — see app/main.py.
    admin_character_id: int | None = None

    eve_sso_auth_url: str = "https://login.eveonline.com/v2/oauth/authorize"
    eve_sso_token_url: str = "https://login.eveonline.com/v2/oauth/token"
    eve_sso_verify_url: str = "https://login.eveonline.com/oauth/verify"
    eve_esi_base: str = "https://esi.evetech.net/latest"

    killmails_enabled: bool = False
    killmail_dashboard_enabled: bool = False
    killmail_battles_enabled: bool = False
    killmail_stream_enabled: bool = False  # killmail.stream live consumer for big-battle banner
    killmail_gc_enabled: bool = False  # off by default so /intel/kills has full history
    everef_ingest_enabled: bool = False  # historical backfill from data.everef.net

    # Ops/backfill progress ping (see everef_ingest.py). Optional; no-op unset.
    # Named DISCORD_WEBHOOK to match the host-level ops tooling that shares it.
    discord_webhook: str = ""

    # Discord relay for user-facing alert notifications (structure attacks,
    # fuel alerts, etc — see app/notify/discord.py). Deliberately a SEPARATE
    # variable from discord_webhook above: that one is the shared ops/backfill
    # ping used by everef_ingest.py, and reusing it here would silently start
    # relaying alerts the moment DISCORD_WEBHOOK is set for unrelated reasons.
    discord_webhook_url: str = ""  # optional — unset means the relay no-ops
    discord_alert_types: str = "structure_attack,structure_fuel"  # comma-separated opt-in list

    # Contact address advertised in the outbound HTTP User-Agent. Third-party
    # APIs (ESI, zKillboard, EVE-Offline, EVE-Scout) require a real contact so
    # their operators can reach the person behind the traffic — an empty value
    # falls back to DEFAULT_CONTACT rather than sending nothing.
    # Self-hosters: set CONTACT_EMAIL to your OWN address so remote operators
    # can reach YOU about your instance's traffic, not the project tracker.
    contact_email: str = DEFAULT_CONTACT

    # Optional link to a Wanderer wormhole-mapper instance, surfaced as an
    # external item in the Map nav group. Empty (the default) drops the item
    # from the nav entirely — see app/nav.py. Deliberately not defaulted to any
    # particular deployment: a self-hoster's nav should not link to someone
    # else's mapper.
    wanderer_url: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def user_agent(suffix: str = "") -> str:
    """Build the outbound HTTP User-Agent, e.g. ``Vigilant/1.2.3 (you@example.com)``.

    The version half comes from ``Settings.version`` — the build tag baked into
    the image by release CI, or ``dev`` for a source build.

    Pass ``suffix="backfill"`` for bulk historical fetches so remote operators
    can tell one-shot backfill traffic apart from interactive requests:
    ``Vigilant/1.0 backfill (you@example.com)``.

    Call this at REQUEST time (inside the function making the HTTP call), not at
    module import time — it reads ``get_settings()``, which loads ``.env`` and is
    ``lru_cache``d, so touching it during import would couple module import order
    to config availability.
    """
    contact = (get_settings().contact_email or "").strip() or DEFAULT_CONTACT
    version = (get_settings().version or "dev").strip().lstrip("v") or "dev"
    product = f"{UA_PRODUCT_NAME}/{version} {suffix}".rstrip()
    return f"{product} ({contact})"
