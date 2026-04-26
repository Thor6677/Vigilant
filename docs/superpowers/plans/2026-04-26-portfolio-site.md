# Portfolio Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a personal portfolio/blog site at `thunderborn.dev` with a dark/minimal aesthetic, hub-style navigation, and a Vigilant tools card.

**Architecture:** Astro 5 static site in a new `~/Documents/Personal/Portfolio` repo. On the VPS, a self-contained portfolio Docker stack (`/opt/portfolio/`) runs its own nginx container serving the built static files. It joins the shared external `web` Docker network so the existing vigilant nginx (already the shared reverse proxy for `mapper.thunderborn.dev`) can proxy `thunderborn.dev` to it via a single new vhost file.

**Tech Stack:** Astro 5 (static output), plain CSS custom properties, Astro Content Collections for blog, nginx:alpine Docker container, Cloudflare proxy + existing origin cert for TLS.

---

## File Map

**New repo: `~/Documents/Personal/Portfolio/`**

| File | Purpose |
|------|---------|
| `astro.config.mjs` | Static output, canonical site URL |
| `src/styles/global.css` | CSS tokens, reset, nav/footer base styles |
| `src/layouts/Base.astro` | Nav + footer shell used by all pages |
| `src/pages/index.astro` | Hub homepage — hero + 3 section cards |
| `src/pages/tools.astro` | Tools list — Vigilant card |
| `src/pages/blog/index.astro` | Blog index — placeholder until posts exist |
| `src/pages/blog/[id].astro` | Individual post page (static paths from Content Collections) |
| `src/content.config.ts` | Content Collections schema (title, date, optional summary) |
| `src/content/blog/.gitkeep` | Keeps empty blog dir in git |
| `src/pages/about.astro` | About page — bio + links |
| `docker-compose.yml` | Portfolio nginx:alpine container on the shared `web` network |
| `nginx.conf` | Portfolio nginx config — serve static files, `try_files` routing |
| `deploy.sh` | Build Astro, rsync `dist/` to VPS, done |

**In `~/Documents/Personal/vigilant-vps/` (existing repo):**

| File | Purpose |
|------|---------|
| `nginx/thunderborn.conf` | New vhost: proxy `thunderborn.dev` → portfolio container |

---

## Task 0: Initialize Astro project and repo

**Goal:** Astro 5 project scaffolded in `~/Documents/Personal/Portfolio`, configured for static output, committed to git.

**Files:**
- Create: `~/Documents/Personal/Portfolio/` (entire scaffold)
- Create: `astro.config.mjs`

**Acceptance Criteria:**
- [ ] `npm run build` completes with no errors
- [ ] `dist/index.html` exists after build
- [ ] Git repo initialized with initial commit

**Verify:** `npm run build && ls dist/index.html` → prints path, no build errors

**Steps:**

- [ ] **Step 1: Scaffold the project**

```bash
cd ~/Documents/Personal
npm create astro@latest Portfolio
```

When prompted, select:
- **Template:** "A basic, minimal starter (recommended)"
- **TypeScript:** Yes → **Strictest**
- **Install dependencies:** Yes
- **Initialize git repo:** No (we do it in Step 3)

- [ ] **Step 2: Replace `astro.config.mjs`**

```js
// astro.config.mjs
import { defineConfig } from 'astro/config';

export default defineConfig({
  output: 'static',
  site: 'https://thunderborn.dev',
});
```

- [ ] **Step 3: Verify build**

```bash
cd ~/Documents/Personal/Portfolio
npm run build
```

Expected: no errors, `dist/index.html` present.

- [ ] **Step 4: Initialize git and commit**

```bash
git init
git add .
git commit -m "init: scaffold Astro project"
```

---

## Task 1: Base layout and global styles

**Goal:** Dark/minimal CSS theme and `Base.astro` layout shell with nav and footer, used by all pages.

**Files:**
- Create: `src/styles/global.css`
- Create (or replace): `src/layouts/Base.astro`

**Acceptance Criteria:**
- [ ] `npm run build` succeeds
- [ ] `astro dev` shows nav with `thunderborn.dev` wordmark + Tools/Blog/About links
- [ ] Background is `#0f0f0f`, body text `#fafafa`
- [ ] Footer shows GitHub and Vigilant links

**Verify:** `npm run build` → no errors

**Steps:**

- [ ] **Step 1: Create `src/styles/global.css`**

```css
:root {
  --bg: #0f0f0f;
  --surface: #1a1a1a;
  --border: #262626;
  --text: #fafafa;
  --text-secondary: #a3a3a3;
  --text-muted: #525252;
}

*, *::before, *::after { box-sizing: border-box; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, sans-serif;
  margin: 0;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

a { color: inherit; text-decoration: none; }

nav.site-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1rem 1.5rem;
  border-bottom: 1px solid var(--border);
}

.wordmark {
  font-weight: 600;
  font-size: 0.9rem;
  letter-spacing: 0.02em;
}

.nav-links {
  display: flex;
  gap: 1.5rem;
  font-size: 0.8rem;
  color: var(--text-muted);
}

.nav-links a:hover { color: var(--text-secondary); }

main {
  flex: 1;
  padding: 2.5rem 1.5rem;
  max-width: 800px;
  margin: 0 auto;
  width: 100%;
}

footer.site-footer {
  display: flex;
  gap: 1rem;
  padding: 1rem 1.5rem;
  border-top: 1px solid var(--border);
  font-size: 0.75rem;
  color: var(--text-muted);
}

footer.site-footer a:hover { color: var(--text-secondary); }
```

- [ ] **Step 2: Create `src/layouts/Base.astro`**

The minimal scaffold may already have a `layouts/` dir — replace `Layout.astro` or create `Base.astro`:

```astro
---
import '../styles/global.css';

interface Props {
  title: string;
}
const { title } = Astro.props;
---
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{title} — thunderborn.dev</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  </head>
  <body>
    <nav class="site-nav">
      <a href="/" class="wordmark">thunderborn.dev</a>
      <div class="nav-links">
        <a href="/tools">Tools</a>
        <a href="/blog">Blog</a>
        <a href="/about">About</a>
      </div>
    </nav>
    <main>
      <slot />
    </main>
    <footer class="site-footer">
      <a href="https://github.com/Thor6677" target="_blank" rel="noopener">GitHub</a>
      <a href="https://vigilant.thunderborn.dev" target="_blank" rel="noopener">Vigilant ↗</a>
    </footer>
  </body>
</html>
```

- [ ] **Step 3: Build and verify**

```bash
npm run build
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add src/styles/global.css src/layouts/Base.astro
git commit -m "feat: add Base layout and global dark theme"
```

---

## Task 2: Homepage

**Goal:** Hub homepage at `/` with hero section and three clickable cards linking to Tools, Blog, About.

**Files:**
- Modify: `src/pages/index.astro`

**Acceptance Criteria:**
- [ ] Hero shows name, tagline, and bio
- [ ] Three cards: Tools, Blog, About — each links to its section
- [ ] Cards have hover border brightening
- [ ] Responsive: cards stack to single column below 600px

**Verify:** `npm run build` → no errors; `astro dev` and open `http://localhost:4321`

**Steps:**

- [ ] **Step 1: Replace `src/pages/index.astro`**

```astro
---
import Base from '../layouts/Base.astro';
---
<Base title="Home">
  <section class="hero">
    <h1>Iian Johnson</h1>
    <p class="tagline">Software developer · EVE Online toolmaker</p>
    <p class="bio">
      I build software tools that make complex systems legible —
      mostly EVE Online infrastructure, occasionally other things.
    </p>
  </section>

  <nav class="hub-cards">
    <a href="/tools" class="hub-card">
      <span class="hub-icon">🛠</span>
      <h2>Tools</h2>
      <p>Public projects and utilities I've shipped</p>
    </a>
    <a href="/blog" class="hub-card">
      <span class="hub-icon">📝</span>
      <h2>Blog</h2>
      <p>Dev writeups and notes on what I'm building</p>
    </a>
    <a href="/about" class="hub-card">
      <span class="hub-icon">👤</span>
      <h2>About</h2>
      <p>Background, interests, and how to reach me</p>
    </a>
  </nav>
</Base>

<style>
  .hero {
    padding-bottom: 2rem;
    border-bottom: 1px solid var(--border);
    margin-bottom: 2rem;
  }

  h1 {
    font-size: 1.5rem;
    font-weight: 600;
    margin: 0 0 0.25rem;
  }

  .tagline {
    color: var(--text-muted);
    font-size: 0.85rem;
    margin: 0 0 1rem;
  }

  .bio {
    color: var(--text-secondary);
    font-size: 0.875rem;
    line-height: 1.7;
    max-width: 480px;
    margin: 0;
  }

  .hub-cards {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.75rem;
  }

  .hub-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.25rem;
    display: block;
    transition: border-color 0.15s;
  }

  .hub-card:hover { border-color: var(--text-muted); }

  .hub-icon { font-size: 1.25rem; }

  .hub-card h2 {
    font-size: 0.875rem;
    font-weight: 600;
    margin: 0.5rem 0 0.25rem;
    color: var(--text);
  }

  .hub-card p {
    font-size: 0.8rem;
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0;
  }

  @media (max-width: 600px) {
    .hub-cards { grid-template-columns: 1fr; }
  }
</style>
```

- [ ] **Step 2: Build and verify**

```bash
npm run build
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/pages/index.astro
git commit -m "feat: add homepage with hero and hub cards"
```

---

## Task 3: Tools page

**Goal:** `/tools` page with a Vigilant card showing name, live link, description, and tech tags.

**Files:**
- Create: `src/pages/tools.astro`

**Acceptance Criteria:**
- [ ] Page title "Tools" with subtitle
- [ ] Vigilant card with name, live link button, description, Python/React/EVE ESI tags
- [ ] Live link opens `https://vigilant.thunderborn.dev` in new tab

**Verify:** `npm run build` → no errors; `astro dev` and open `http://localhost:4321/tools`

**Steps:**

- [ ] **Step 1: Create `src/pages/tools.astro`**

```astro
---
import Base from '../layouts/Base.astro';
---
<Base title="Tools">
  <h1>Tools</h1>
  <p class="subtitle">Things I've built and shipped</p>

  <div class="tool-card">
    <div class="tool-header">
      <span class="tool-name">Vigilant</span>
      <a
        href="https://vigilant.thunderborn.dev"
        target="_blank"
        rel="noopener"
        class="live-link"
      >Live ↗</a>
    </div>
    <p class="tool-desc">
      EVE Online companion dashboard — multi-character overview, interactive
      star map, ship fitting tool, intel, and more.
    </p>
    <div class="tool-tags">
      <span class="tag">Python</span>
      <span class="tag">React</span>
      <span class="tag">EVE ESI</span>
    </div>
  </div>
</Base>

<style>
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.25rem; }

  .subtitle { color: var(--text-muted); font-size: 0.85rem; margin: 0 0 1.5rem; }

  .tool-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.25rem;
  }

  .tool-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.75rem;
  }

  .tool-name { font-weight: 600; font-size: 0.95rem; }

  .live-link {
    font-size: 0.75rem;
    color: var(--text-muted);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 0.2rem 0.5rem;
    transition: color 0.15s;
  }

  .live-link:hover { color: var(--text-secondary); }

  .tool-desc {
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.6;
    margin: 0 0 0.75rem;
  }

  .tool-tags { display: flex; gap: 0.4rem; flex-wrap: wrap; }

  .tag {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 0.15rem 0.4rem;
    font-size: 0.7rem;
    color: var(--text-muted);
  }
</style>
```

- [ ] **Step 2: Build and verify**

```bash
npm run build
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add src/pages/tools.astro
git commit -m "feat: add tools page with Vigilant card"
```

---

## Task 4: Blog (Content Collections + placeholder + post route)

**Goal:** Content Collections schema wired up, `/blog` renders a "coming soon" placeholder when empty, `/blog/[id]` renders individual posts when they exist.

**Files:**
- Create: `src/content.config.ts`
- Create: `src/content/blog/.gitkeep`
- Create: `src/pages/blog/index.astro`
- Create: `src/pages/blog/[id].astro`

**Acceptance Criteria:**
- [ ] `npm run build` succeeds with no blog posts present
- [ ] `/blog` shows placeholder "Posts coming soon" card when collection is empty
- [ ] Adding a test `.md` file to `src/content/blog/` and rebuilding: post appears in the list and `/blog/<id>` renders it
- [ ] Remove the test post after verifying

**Verify:** `npm run build` → no errors; `astro dev` → `http://localhost:4321/blog` shows placeholder

**Steps:**

- [ ] **Step 1: Create `src/content.config.ts`**

```ts
import { defineCollection, z } from 'astro:content';

const blog = defineCollection({
  type: 'content',
  schema: z.object({
    title: z.string(),
    date: z.date(),
    summary: z.string().optional(),
  }),
});

export const collections = { blog };
```

- [ ] **Step 2: Create `src/content/blog/.gitkeep`**

```bash
mkdir -p src/content/blog
touch src/content/blog/.gitkeep
```

- [ ] **Step 3: Create `src/pages/blog/index.astro`**

```astro
---
import Base from '../../layouts/Base.astro';
import { getCollection } from 'astro:content';

const posts = await getCollection('blog');
const sorted = posts.sort(
  (a, b) => b.data.date.valueOf() - a.data.date.valueOf()
);
---
<Base title="Blog">
  <h1>Blog</h1>
  <p class="subtitle">Dev writeups and notes</p>

  {sorted.length === 0 ? (
    <div class="empty">Posts coming soon</div>
  ) : (
    <ul class="post-list">
      {sorted.map(post => (
        <li>
          <a href={`/blog/${post.id}`} class="post-link">
            <span class="post-title">{post.data.title}</span>
            <span class="post-date">
              {post.data.date.toLocaleDateString('en-US', {
                year: 'numeric', month: 'short', day: 'numeric'
              })}
            </span>
          </a>
        </li>
      ))}
    </ul>
  )}
</Base>

<style>
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.25rem; }
  .subtitle { color: var(--text-muted); font-size: 0.85rem; margin: 0 0 1.5rem; }

  .empty {
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: 6px;
    padding: 2rem;
    text-align: center;
    color: var(--text-muted);
    font-size: 0.875rem;
  }

  .post-list { list-style: none; padding: 0; margin: 0; }
  .post-list li { border-bottom: 1px solid var(--border); }
  .post-list li:last-child { border-bottom: none; }

  .post-link {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem 0;
    font-size: 0.875rem;
    color: var(--text-secondary);
    transition: color 0.15s;
  }

  .post-link:hover .post-title { color: var(--text); }
  .post-date { color: var(--text-muted); font-size: 0.75rem; flex-shrink: 0; }
</style>
```

- [ ] **Step 4: Create `src/pages/blog/[id].astro`**

```astro
---
import Base from '../../layouts/Base.astro';
import { getCollection, render } from 'astro:content';

export async function getStaticPaths() {
  const posts = await getCollection('blog');
  return posts.map(post => ({ params: { id: post.id } }));
}

const { id } = Astro.params;
const posts = await getCollection('blog');
const post = posts.find(p => p.id === id)!;
const { Content } = await render(post);
---
<Base title={post.data.title}>
  <article>
    <h1>{post.data.title}</h1>
    <time class="date">
      {post.data.date.toLocaleDateString('en-US', {
        year: 'numeric', month: 'long', day: 'numeric'
      })}
    </time>
    <div class="content">
      <Content />
    </div>
  </article>
</Base>

<style>
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 0.25rem; }
  .date { color: var(--text-muted); font-size: 0.8rem; display: block; margin-bottom: 2rem; }

  .content { font-size: 0.875rem; line-height: 1.8; color: var(--text-secondary); }
  .content h2 { font-size: 1rem; font-weight: 600; color: var(--text); margin-top: 2rem; }
  .content a { color: var(--text-secondary); text-decoration: underline; }
  .content code {
    background: var(--surface);
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    font-size: 0.8em;
  }
  .content pre {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem;
    overflow-x: auto;
  }
  .content pre code { background: none; padding: 0; }
</style>
```

- [ ] **Step 5: Smoke-test with a temporary post**

Create `src/content/blog/test-post.md`:
```markdown
---
title: Test Post
date: 2026-04-26
---

This is a test post to verify the blog route works.
```

Run:
```bash
npm run build
```

Expected: no errors, `dist/blog/test-post/index.html` exists.

Delete `src/content/blog/test-post.md` after verifying.

- [ ] **Step 6: Final build and commit**

```bash
npm run build
git add src/content.config.ts src/content/blog/.gitkeep src/pages/blog/
git commit -m "feat: add blog with Content Collections and placeholder"
```

---

## Task 5: About page

**Goal:** `/about` page with a bio paragraph and links to GitHub and Vigilant.

**Files:**
- Create: `src/pages/about.astro`

**Acceptance Criteria:**
- [ ] Page renders with bio text, GitHub link, Vigilant link
- [ ] Both links open in new tab with `rel="noopener"`
- [ ] `npm run build` succeeds

**Verify:** `npm run build` → no errors; `astro dev` → `http://localhost:4321/about`

**Steps:**

- [ ] **Step 1: Create `src/pages/about.astro`**

```astro
---
import Base from '../layouts/Base.astro';
---
<Base title="About">
  <h1>About</h1>

  <p class="bio">
    Software developer with a focus on building tools that make complex systems
    legible. I primarily work on EVE Online infrastructure and enjoy the
    challenge of turning dense game data into useful interfaces.
  </p>

  <div class="links">
    <a
      href="https://github.com/Thor6677"
      target="_blank"
      rel="noopener"
      class="link-btn"
    >GitHub ↗</a>
    <a
      href="https://vigilant.thunderborn.dev"
      target="_blank"
      rel="noopener"
      class="link-btn"
    >Vigilant ↗</a>
  </div>
</Base>

<style>
  h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 1.25rem; }

  .bio {
    font-size: 0.875rem;
    line-height: 1.8;
    color: var(--text-secondary);
    max-width: 560px;
    margin: 0 0 1.5rem;
  }

  .links { display: flex; gap: 0.5rem; flex-wrap: wrap; }

  .link-btn {
    font-size: 0.8rem;
    color: var(--text-muted);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 0.3rem 0.75rem;
    transition: color 0.15s;
  }

  .link-btn:hover { color: var(--text-secondary); }
</style>
```

- [ ] **Step 2: Build and verify**

```bash
npm run build
```

Expected: no errors, `dist/about/index.html` exists.

- [ ] **Step 3: Commit**

```bash
git add src/pages/about.astro
git commit -m "feat: add about page"
```

---

## Task 6: Deploy infrastructure

**Goal:** Portfolio Docker stack on VPS at `/opt/portfolio/`, `deploy.sh` that rsyncs built files there, and a `thunderborn.conf` vhost in the vigilant-vps nginx that proxies to the portfolio container. First deploy verifies the site is live at `thunderborn.dev`.

**Files:**
- Create: `~/Documents/Personal/Portfolio/docker-compose.yml`
- Create: `~/Documents/Personal/Portfolio/nginx.conf`
- Create: `~/Documents/Personal/Portfolio/deploy.sh`
- Create: `~/Documents/Personal/vigilant-vps/nginx/thunderborn.conf`

**Acceptance Criteria:**
- [ ] `./deploy.sh` builds and rsyncs successfully to VPS
- [ ] Portfolio container is running: `docker ps | grep portfolio`
- [ ] Vigilant nginx config test passes after vhost is added
- [ ] `https://thunderborn.dev` serves the homepage with HTTP 200

**Verify:** `curl -s -o /dev/null -w "%{http_code}" https://thunderborn.dev` → `200`

**Steps:**

- [ ] **Step 1: Create `docker-compose.yml` in Portfolio repo**

The portfolio nginx container mounts the built static files from `/opt/portfolio/www/` and joins the shared `web` network so the vigilant nginx can reach it by container name.

```yaml
services:
  portfolio:
    image: nginx:alpine
    container_name: portfolio
    restart: unless-stopped
    volumes:
      - ./www:/usr/share/nginx/html:ro
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    networks:
      - web
    security_opt:
      - no-new-privileges:true

networks:
  web:
    external: true
    name: web
```

- [ ] **Step 2: Create `nginx.conf` in Portfolio repo**

This is the portfolio container's own nginx config — serves static files with Astro-compatible routing (files are pre-built, no SPA fallback needed):

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri.html $uri/ =404;
    }
}
```

- [ ] **Step 3: Create `deploy.sh` in Portfolio repo**

```bash
#!/usr/bin/env bash
set -euo pipefail

VPS="ijohnson@146.190.140.112"
VPS_DIR="/opt/portfolio"

echo "==> Building..."
npm run build

echo "==> Syncing to $VPS:$VPS_DIR/www/..."
rsync -avz --delete dist/ "$VPS:$VPS_DIR/www/"

echo "==> Done. https://thunderborn.dev"
```

Make executable and commit:
```bash
chmod +x deploy.sh
git add docker-compose.yml nginx.conf deploy.sh
git commit -m "feat: add Docker stack and deploy script"
```

- [ ] **Step 4: Bootstrap the portfolio stack on the VPS (one-time)**

```bash
# Create the directory and copy the Docker files
ssh ijohnson@146.190.140.112 "mkdir -p /opt/portfolio/www"
scp docker-compose.yml nginx.conf ijohnson@146.190.140.112:/opt/portfolio/

# Start the portfolio container
ssh ijohnson@146.190.140.112 "cd /opt/portfolio && docker compose up -d"
```

Verify it's running:
```bash
ssh ijohnson@146.190.140.112 "docker ps | grep portfolio"
```

Expected: a line showing `portfolio` container with status `Up`.

- [ ] **Step 5: Create `nginx/thunderborn.conf` in the vigilant-vps repo**

In `~/Documents/Personal/vigilant-vps/nginx/thunderborn.conf`:

```nginx
server {
    listen 80;
    server_name thunderborn.dev www.thunderborn.dev;

    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    http2 on;
    server_name thunderborn.dev www.thunderborn.dev;

    ssl_certificate /etc/nginx/ssl/origin.pem;
    ssl_certificate_key /etc/nginx/ssl/origin.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_tickets off;

    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    resolver 127.0.0.11 valid=30s ipv6=off;

    location / {
        proxy_pass http://portfolio;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Also update `docker-compose.yml` in the vigilant-vps repo to mount the new conf file into the nginx container. Add this line under the nginx `volumes:` block:

```yaml
      - ./nginx/thunderborn.conf:/etc/nginx/conf.d/thunderborn.conf:ro
```

Commit both changes:
```bash
cd ~/Documents/Personal/vigilant-vps
git add nginx/thunderborn.conf docker-compose.yml
git commit -m "nginx: add thunderborn.dev vhost proxying to portfolio container"
```

- [ ] **Step 6: Deploy vigilant-vps changes and recreate nginx**

```bash
git push origin main
ssh ijohnson@146.190.140.112 "cd /opt/vigilant && git pull"
```

Recreate the nginx container so it picks up the new bind-mounted conf file (reload reads the old inode, recreate is required):
```bash
ssh ijohnson@146.190.140.112 "cd /opt/vigilant && docker compose up -d --force-recreate nginx"
```

Verify nginx config is valid:
```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-nginx-1 nginx -t"
```

Expected: `nginx: configuration file /etc/nginx/nginx.conf test is successful`

- [ ] **Step 7: Add Cloudflare DNS record**

In the Cloudflare dashboard for `thunderborn.dev`:
- Add an **A record**: name `@`, value `146.190.140.112`, **Proxied** (orange cloud)
- Add a **CNAME**: name `www`, value `thunderborn.dev`, **Proxied**

- [ ] **Step 8: Run first deploy**

```bash
cd ~/Documents/Personal/Portfolio
./deploy.sh
```

Expected: Astro build output, then rsync output, then `Done. https://thunderborn.dev`

- [ ] **Step 9: Verify site is live**

```bash
curl -s -o /dev/null -w "%{http_code}" https://thunderborn.dev
```

Expected: `200`

Open `https://thunderborn.dev` in a browser and confirm the homepage renders correctly.
