# Security Policy

Vigilant is **self-hosted**. Every operator runs their own instance, registers their own EVE developer application, and owns their own SQLite database. There is no central hosted service and no shared store of other people's data. The realistic worst case for most vulnerabilities is therefore bounded: an attacker compromises one operator's own instance and the encrypted ESI tokens for the characters that operator added. That's still worth fixing properly — it just shapes how severity gets triaged here.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:

**https://github.com/Thor6677/Vigilant/security/advisories/new**

(Also reachable from the **Security** tab of the repository.) This keeps the report visible only to the maintainer until a fix is out, and requires no email address to be published.

**Please do not open a public issue, discussion, or pull request for a suspected vulnerability.** Public issues are the right place for ordinary bugs, not for anything exploitable against running instances.

A useful report includes:

- **What breaks** — the vulnerability class and the concrete impact
- **Where** — file and function, or the route/endpoint
- **How to reproduce** — steps, a request, or a short proof of concept
- **Preconditions** — what access the attacker needs first (unauthenticated? logged-in ordinary user? admin? local shell?)
- **Version** — the commit SHA you tested

## Scope

**In scope:**

- **Authentication and session handling** — the EVE SSO OAuth2 flow, OAuth state/CSRF validation on the callback, signed session cookies (`HttpOnly`, `SameSite=Lax`), session fixation or forgery
- **Token storage** — the Fernet encryption of ESI access/refresh tokens at rest and the PBKDF2 derivation of that key from `SECRET_KEY`
- **Per-user data isolation** — any route or query that lets one authenticated user read or modify another user's characters, corps, fittings, skill plans, or timers
- **Admin bootstrap and privilege escalation** — the `ADMIN_CHARACTER_ID` first-admin promotion, role checks on `/admin`, and structure-timer ACL/role enforcement
- **Injection and untrusted input** — SQL injection, template injection, XSS (including via pasted D-Scan/EFT/killmail content and shared public links), path traversal, SSRF
- **The shipped hardening posture** — anything that defeats the container hardening in `docker-compose.yml` (read-only filesystem, `cap_drop: ALL`, `no-new-privileges`), the privilege drop in `docker-entrypoint.sh`, or the security headers in `docs/nginx-sample.conf`

**Out of scope:**

- **Self-inflicted misconfiguration** — a weak or reused `SECRET_KEY`, a world-readable `.env`, `DEBUG=true` in production, running without TLS, or exposing port 8000 straight to the internet. These are covered by the README's Production Checklist and are the operator's responsibility.
- **Vulnerabilities in EVE Online's own services** — ESI, EVE SSO, or Fenris Creations' infrastructure. Report those to [the EVE developer portal](https://developers.eveonline.com/). The same goes for third-party data sources like zKillboard.
- **Denial of service by hammering a self-hosted instance** — resource exhaustion from a flood of requests against an instance you control, or ESI rate-limit exhaustion. Rate limiting belongs in the operator's reverse proxy.
- **Anything requiring an already-compromised host** — attacks that start from shell access, root, or the ability to read `/data` or `.env` directly. If you're already there, you have the database.
- **Missing hardening with no demonstrated impact** — header/CSP tuning suggestions, dependency versions with no reachable exploit path, or scanner output without a working scenario. These are welcome as regular issues.

## What to expect

This is a hobby project maintained by one person in their spare time. There is **no SLA and no bug bounty** — please don't expect either.

Honestly, what you can expect:

- An acknowledgement once the report is seen
- An assessment of whether it's in scope and how severe it looks
- A fix on a best-effort basis, prioritized by real impact. Serious auth, isolation, or token-exposure issues get worked on first; low-impact hardening may sit for a while
- Credit in the advisory and commit message unless you'd rather stay anonymous

Please give a reasonable window before disclosing publicly. If a report goes unanswered for 90 days, treat yourself as free to disclose.

## Supported versions

Only current `main` is supported. There are no release tags, no maintained branches, and no backports — fixes land on `main` and operators pick them up by pulling and rebuilding:

```bash
git pull origin main
docker compose down && docker compose up -d --build
```

## Hardening your own instance

If you run Vigilant, start with the **Production Checklist** in [README.md](README.md#production-checklist): HTTPS with a valid certificate, a strong `SECRET_KEY` that never changes while the database has active users, `chmod 600 .env`, `.env` never committed, `DEBUG=false`, and regular database backups. Set `ADMIN_CHARACTER_ID` to your own character id **before** first launch so an unrelated first signup can't take the instance. The README's Security section documents what the app does for you and what your reverse proxy still has to do.
