/* Vigilant Ambient — flying through New Eden with live sov colors and kill blips.
   Dependency-free ES module. Canvas 2D. See design spec 2026-07-02. */

const ESI_SOV_URL = 'https://esi.evetech.net/latest/sovereignty/map/?datasource=tranquility';
const SOV_CACHE_KEY = 'vg-ambient-sov-v1';
const SOV_TTL_MS = 24 * 60 * 60 * 1000;

const FACTION_COLORS = {
  500001: [74, 144, 217],  // Caldari State
  500002: [179, 74, 58],   // Minmatar Republic
  500003: [230, 190, 90],  // Amarr Empire
  500004: [88, 191, 117],  // Gallente Federation
  500026: [200, 60, 60],   // Triglavian
};
const NEUTRAL = [190, 195, 205];

function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0]; else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x]; else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c]; else [r, g, b] = [c, 0, x];
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}
export function allianceColor(id) { return hslToRgb((id * 137.508) % 360, 0.62, 0.58); }

export function normalizeSystems(raw) {
  // raw: array of {id, name, sec, x3, y3, z3} (frontend/public/data/systems.json shape)
  const n = raw.length;
  let cx = 0, cy = 0, cz = 0;
  for (const s of raw) { cx += s.x3; cy += s.y3; cz += s.z3; }
  cx /= n; cy /= n; cz /= n;
  let ext = 0;
  for (const s of raw) ext = Math.max(ext, Math.abs(s.x3 - cx), Math.abs(s.z3 - cz));
  const k = 1000 / ext;
  return raw.map((s) => ({
    id: s.id, name: s.name,
    x: (s.x3 - cx) * k, y: (s.y3 - cy) * k, z: (s.z3 - cz) * k,
  }));
}

async function loadSovColors(systems) {
  const byId = new Map(systems.map((s, i) => [s.id, i]));
  const cols = systems.map(() => NEUTRAL);
  let sov = null;
  try {
    const cached = JSON.parse(localStorage.getItem(SOV_CACHE_KEY) || 'null');
    if (cached && Date.now() - cached.t < SOV_TTL_MS) sov = cached.d;
  } catch (e) { /* localStorage unavailable — fall through to fetch */ }
  if (!sov) {
    try {
      const r = await fetch(ESI_SOV_URL);
      if (!r.ok) return cols;
      sov = await r.json();
      try { localStorage.setItem(SOV_CACHE_KEY, JSON.stringify({ t: Date.now(), d: sov })); } catch (e) { /* quota */ }
    } catch (e) { return cols; }
  }
  for (const e of sov) {
    const i = byId.get(e.system_id);
    if (i === undefined) continue;
    if (e.alliance_id) cols[i] = allianceColor(e.alliance_id);
    else if (e.faction_id && FACTION_COLORS[e.faction_id]) cols[i] = FACTION_COLORS[e.faction_id];
  }
  return cols;
}

export function mount(el, options = {}) {
  const opts = {
    systemsUrl: '/static/data/systems.json',
    killSource: { type: 'simulate' },
    minWidth: 768,
    fpsCap: 30,
    speed: 0.00012,
    ...options,
  };

  const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || window.innerWidth < opts.minWidth) return { destroy() {} };

  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:var(--z-ambient,-1);pointer-events:none;';
  el.appendChild(canvas);
  const ctx = canvas.getContext('2d');

  let W = 0, H = 0, raf = 0, killTimer = 0, destroyed = false;
  let systems = [], cols = [], kill = null;
  let t = 0, last = 0;
  const frameMs = 1000 / opts.fpsCap;
  const RX = 520, RZ = 420, CAMY = 120, FOV = 700, NEAR = 20, FAR = 1600;

  function resize() { W = canvas.width = innerWidth; H = canvas.height = innerHeight; }
  resize();
  addEventListener('resize', resize);

  function camPos(tt) {
    const a = tt * Math.PI * 2;
    return [Math.cos(a) * RX, CAMY + Math.sin(a * 3) * 30, Math.sin(a) * RZ];
  }

  function frame(now) {
    if (destroyed) return;
    raf = requestAnimationFrame(frame);
    if (document.hidden || now - last < frameMs) return;
    last = now;
    t += opts.speed;
    const cam = camPos(t), ahead = camPos(t + 0.012);
    let fx = ahead[0] - cam[0], fy = ahead[1] - cam[1] - 40, fz = ahead[2] - cam[2];
    const fl = Math.hypot(fx, fy, fz); fx /= fl; fy /= fl; fz /= fl;
    let rx = fz, rz = -fx;
    const rl = Math.hypot(rx, rz) || 1; rx /= rl; rz /= rl;
    const ux = -rz * fy, uy = rz * fx - rx * fz, uz = rx * fy;

    ctx.fillStyle = '#04040a'; ctx.fillRect(0, 0, W, H);
    const g = ctx.createRadialGradient(W * 0.7, H * 0.35, 0, W * 0.7, H * 0.35, W * 0.6);
    g.addColorStop(0, 'rgba(90,70,140,.05)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    for (let i = 0; i < systems.length; i++) {
      const p = systems[i];
      const dx = p.x - cam[0], dy = p.y - cam[1], dz = p.z - cam[2];
      const z = dx * fx + dy * fy + dz * fz;
      if (z < NEAR || z > FAR) { if (kill) kill[i] *= 0.98; continue; }
      const x = dx * rx + dz * rz;
      const y = dx * ux + dy * uy + dz * uz;
      const k = FOV / z;
      const sx = W / 2 + x * k, sy = H / 2 - y * k;
      if (sx < -30 || sx > W + 30 || sy < -30 || sy > H + 30) { if (kill) kill[i] *= 0.98; continue; }
      let fog = 1 - z / FAR; fog *= fog;
      const rad = Math.max(0.5, 2.6 * k * 0.55);
      const c = cols[i] || NEUTRAL;
      ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${(0.25 + 0.65 * fog).toFixed(3)})`;
      ctx.beginPath(); ctx.arc(sx, sy, rad, 0, 7); ctx.fill();
      if (k > 0.5) {
        ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${(0.08 * fog).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(sx, sy, rad * 3.2, 0, 7); ctx.fill();
      }
      if (kill && kill[i] > 0.01) {
        const kr = (1 - kill[i]) * 46 * Math.min(k, 2) + rad + 2;
        ctx.strokeStyle = `rgba(255,70,70,${(kill[i] * 0.95).toFixed(3)})`; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(sx, sy, kr, 0, 7); ctx.stroke();
        ctx.fillStyle = `rgba(255,90,90,${(kill[i] * 0.9).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(sx, sy, rad + 2, 0, 7); ctx.fill();
        kill[i] *= 0.988;
      }
    }
  }

  function flare(systemId) {
    if (!kill) return;
    for (let i = 0; i < systems.length; i++) {
      if (systems[i].id === systemId) { kill[i] = 1; return; }
    }
  }

  function startKills() {
    const src = opts.killSource;
    if (src.type === 'simulate') {
      const tick = () => {
        if (destroyed) return;
        // flare a random system roughly ahead of the camera
        const cam = camPos(t), ahead = camPos(t + 0.012);
        let fx = ahead[0] - cam[0], fz = ahead[2] - cam[2];
        const fl = Math.hypot(fx, fz); fx /= fl; fz /= fl;
        for (let tries = 0; tries < 40; tries++) {
          const i = (Math.random() * systems.length) | 0;
          const z = (systems[i].x - cam[0]) * fx + (systems[i].z - cam[2]) * fz;
          if (z > 100 && z < 900) { kill[i] = 1; break; }
        }
        killTimer = setTimeout(tick, 900 + Math.random() * 1800);
      };
      killTimer = setTimeout(tick, 700);
    } else if (src.type === 'poll') {
      const tick = async () => {
        if (destroyed) return;
        try {
          const r = await fetch(src.url);
          if (r.ok) (await r.json()).forEach((k) => flare(k.system_id ?? k));
        } catch (e) { /* silent */ }
        killTimer = setTimeout(tick, src.intervalMs || 15000);
      };
      killTimer = setTimeout(tick, 1000);
    }
  }

  (async () => {
    try {
      const r = await fetch(opts.systemsUrl);
      if (!r.ok) return;
      systems = normalizeSystems(await r.json());
      kill = new Float32Array(systems.length);
      cols = await loadSovColors(systems);
      if (destroyed) return;
      startKills();
      raf = requestAnimationFrame(frame);
    } catch (e) { /* no background — page still works */ }
  })();

  return {
    flare,
    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      clearTimeout(killTimer);
      removeEventListener('resize', resize);
      canvas.remove();
    },
  };
}
