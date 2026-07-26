"""The admin overview surfaces the running build version.

Deliberately NOT exposed on /healthz: that endpoint is public and
unauthenticated, and a build version is free reconnaissance. Operators who need
it from a script read the env var directly:

    docker exec -u 10001 <container> printenv VIGILANT_VERSION

(the -u is required — the container drops CAP_DAC_OVERRIDE, so uid 0 cannot
read files owned by the vigilant user.)
"""
import inspect

import app.routes.admin as admin_mod


def test_overview_handler_passes_app_version():
    src = inspect.getsource(admin_mod.admin_overview)
    assert "app_version" in src, "admin_overview must pass app_version to the template"


def test_overview_template_renders_version_tile():
    html = open("app/templates/partials/admin_overview.html").read()
    assert "{{ app_version }}" in html
    assert "Version" in html


def test_healthz_does_not_disclose_the_version():
    """Regression guard: a well-meaning change that adds the version to the
    public liveness probe would hand it to anyone scanning the host."""
    import app.main as main_mod
    src = inspect.getsource(main_mod)
    healthz = src[src.index("def healthz"):]
    healthz = healthz[:healthz.index("\n@")] if "\n@" in healthz else healthz
    assert "version" not in healthz.lower()
