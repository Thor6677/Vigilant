# Portfolio Site — Design Spec
_2026-04-26_

## Overview

A personal portfolio/blog site at `thunderborn.dev`. Hub-style layout with dedicated sections for Tools, Blog, and About. Dark/minimal aesthetic. Built with Astro and served as static files from the existing VPS.

---

## Tech Stack

- **Framework**: Astro (static output)
- **Styling**: Plain CSS with custom properties (dark/minimal theme)
- **Blog content**: Astro Content Collections (Markdown)
- **Hosting**: DigitalOcean VPS (`146.190.140.112`), nginx static file serve
- **DNS/TLS**: Cloudflare proxy — `thunderborn.dev` A record pointing to VPS; existing origin cert covers `*.thunderborn.dev`

---

## Project Structure

New git repo at `~/Documents/Personal/Portfolio`.

```
src/
  layouts/
    Base.astro          # shared nav + footer shell
  pages/
    index.astro         # hub homepage
    tools.astro         # tools list
    about.astro         # about page
    blog/
      index.astro       # blog index
      [slug].astro      # individual post
  content/
    blog/               # .md files (Astro Content Collections)
  styles/
    global.css          # CSS custom properties, base reset
public/                 # static assets (favicon, og image, etc.)
astro.config.mjs
package.json
```

---

## Pages

### `/` — Homepage (hub)

- Nav bar: `thunderborn.dev` wordmark left, `Tools / Blog / About` links right
- Hero: name, one-line tagline, 2–3 sentence bio
- Three equal-width cards linking to `/tools`, `/blog`, `/about` — icon, section title, short descriptor
- Footer: GitHub link, `vigilant.thunderborn.dev` link

### `/tools`

- Page title + subtitle
- One card per tool (just Vigilant at launch):
  - Tool name, live link button
  - Short description
  - Tech tags (Python, React, EVE ESI)
- Card layout scales to multiple tools when added later

### `/blog`

- Page title + subtitle
- Placeholder state: dashed-border empty card reading "Posts coming soon"
- When posts exist: list of post titles, dates, and one-line summaries linking to `/blog/[slug]`

### `/blog/[slug]`

- Generated from Astro Content Collections
- Rendered Markdown, title, date from frontmatter
- No comments, no tags for now

### `/about`

- Short bio paragraph (filled in by user)
- Links: GitHub, `vigilant.thunderborn.dev`

---

## Visual Design

- **Background**: `#0f0f0f`
- **Surface**: `#1a1a1a` (cards, nav underline)
- **Border**: `#262626`
- **Text primary**: `#fafafa`
- **Text secondary**: `#a3a3a3`
- **Text muted**: `#525252`
- **No color accents** — monochromatic grey palette throughout
- **Typography**: `system-ui, sans-serif`; nav/labels in small caps or tight letter-spacing

---

## Deployment

1. Build: `npm run build` (outputs to `dist/`)
2. Deploy: `rsync -avz --delete dist/ ijohnson@146.190.140.112:/var/www/thunderborn/`
3. A `deploy.sh` script at the repo root wraps steps 1–2

**VPS nginx config** (`nginx/thunderborn.conf` — new file added to the vigilant-vps repo):
- `server_name thunderborn.dev www.thunderborn.dev`
- `root /var/www/thunderborn`
- `index index.html`
- `try_files $uri $uri.html $uri/ =404` — Astro pre-generates a real `.html` per route, no SPA fallback needed
- Same SSL cert as other vhosts (`/etc/nginx/ssl/origin.pem`)
- HTTP → HTTPS redirect

Cloudflare: add `thunderborn.dev` A record (proxied) pointing to `146.190.140.112`.

---

## Out of Scope (launch)

- Blog posts (Content Collections wired up, empty)
- Search, tags, RSS feed
- Dark/light mode toggle
- Analytics
- Contact form
