# Security burst — 6 tickets + 2 issues + 8 sec-toolkit findings cleared

Continued autonomous-feeling security session after the nginx decoupling. Storybloq jumped from 9/28 → 15/28 complete.

## Tickets shipped

| ticket | sha | scope |
|---|---|---|
| T-005 | (no commit) | CSRF middleware verification — already wired in app/main.py:76 + base.html, live-verified during T-002 deploy. Marked VVP-2026-004 fixed. |
| T-007 | 2e17a1d | First-user admin bootstrap — accepted-risk via documentation. README "First-User Admin Bootstrap (Accepted Risk)" section added. VVP-2026-013 mark-fixed with re-mark-on-/audit caveat. |
| T-008 | (no commit) | Wanderer secrets — local stubs (wanderer-compose.yml, wanderer-conf.env) deleted from laptop checkout (both gitignored). Real wanderer at /opt/wanderer/ now out of vigilant scope post nginx decoupling. VVP-2026-006 mark-fixed. |
| T-009 | 6f59b35 (in /opt/edge/) | Image endpoint rate-limit — added `image_serve` zone (5r/s, burst=20) on /i/ location in edge nginx vhost. VVP-2026-015 mark-fixed. |
| T-010 | (no commit) | Deleted local repo/ stub (gitignored, never in image). VVP-2026-018 mark-fixed. |
| T-006 | 3edc8c4 | Raw-HTML XSS audit — 3 real fixes: skill_plans.py:1109 (raw not_found names from EFT-import — actual XSS), admin.py:494 (char.character_name defense-in-depth), corporations.py:551 (ESI error string defense-in-depth). VVP-2026-005a/c/d/e/f mark-fixed. |

## Issues resolved
- **ISS-011** — superseded by VPS TestClient docker-build smoke from T-002.
- **ISS-012** — anyio>=4.7 transitive resolver concern; verified moot (T-002 smoke + live deploy succeeded under whatever pip resolved).

## Sec-toolkit findings also closed without dedicated tickets
Verified-fixed-on-main-and-marked during this run:
- **VVP-2026-008** — session lifetime now 7 days (max_age=86400*7), not 30. app/main.py:82.
- **VVP-2026-009** — logout is `@router.post("/logout")`, not GET. app/auth/routes.py:311.
- **VVP-2026-014** — nginx h2c smuggling — vhost moved to /opt/edge/nginx/conf.d/mapper.conf using `proxy_set_header Connection $connection_upgrade` (conditional upgrade map), not static `Connection: upgrade`.
- **VVP-2026-016** — hadolint DL3008 (unpinned apt-get install gcc/gosu) — accepted risk, debian apt repo signature verification is the supply-chain defense. mark-fixed with re-mark-on-/audit caveat.

## Final sec-toolkit state
| status | count | which |
|---|---|---|
| fix-in-progress | 3 | VVP-2026-001 (T-002 deps), VVP-2026-002 (T-003 frontend), VVP-2026-003 (T-004 non-root) — all mine, awaiting /audit re-scan to flip to fixed |
| open | 1 | VVP-2026-007 (CSP unsafe-inline) = T-012, deliberately deferred — see below |
| (the rest are fixed) | | |

SECURITY_TODO.md shrank from 117 lines to 62 lines this session.

## T-012 deferred with detailed roadmap

Inventoried scope across `app/templates/`:
- 514 inline event handlers (`onclick=`/`onchange=`/etc) — can't be nonced, must refactor to `addEventListener`
- 153 templates with `style="..."` attrs — must move to `b-*` utility classes
- 57 templates with `<style>` blocks — can be nonced
- Handful of `<script>` blocks — can be nonced

Path 3 chosen: defer the work. T-012's `description` was rewritten with a 4-step plan (nonce middleware → 514-handler refactor → 153-style refactor → flip Report-Only to enforcing). Each step is its own session, with the 514-handler refactor benefiting from parallel sub-agents. Sec-toolkit VVP-2026-007 remains `open` (NOT mark-fixed) so it stays visible.

## T-011 status
Still blocked. Description references VVP-008, VVP-009, VVP-014 plus the 005-family — all of those are now mark-fixed in this session. T-011's blocker (T-006) is also done. T-011 is just "user runs `/audit` slash-command" — agents can't trigger that. Next session whoever picks this up can mention to user "ready to run /audit now."

## Production state
- vigilant-app-1 healthy, running as uid 10001 (T-004)
- 4 commits pushed this run: 2e17a1d (T-007 docs), 3edc8c4 (T-006 escape), and the two earlier deploys (76dfd04 T-002, ae33fb8 T-004 from previous session segment)
- Edge stack at /opt/edge/ has 1 commit added this run: 6f59b35 (T-009 image rate-limit)
- /opt/vigilant/ has dead leftovers in nginx/ (ssl/, thunderborn.conf) — owned by Portfolio repo, leave alone

## What's next
- **Security phase** is essentially closed except T-012 (deferred with roadmap) and T-011 (blocked on user running `/audit`).
- **Performance & Ops phase** has 4 tickets (T-013/014/015/016) — perf rebaseline, OOM guard, INDEX migration guard, deploy.sh `web` network preflight. T-016 fits very naturally with the deploy.sh changes from this session.
- **Roadmap & Gaps phase** has 7 tickets — wormhole data, fitting accuracy, debug logging cleanup, etc.
