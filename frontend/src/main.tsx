import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'

// Mount into #map-root (from base.html template) or #root (standalone dev)
const container = document.getElementById('map-root') || document.getElementById('root');
createRoot(container!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// ISS-148: an earlier release registered a service worker (scope /map/data/)
// that controls no page and fills a cache nothing reads. Remove it and its
// cache. Once this has shipped for a while, delete this block, map-sw.js and
// the /map/data/ route.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.getRegistrations()
      .then(regs => Promise.all(
        regs
          .filter(r => new URL(r.scope).pathname === '/map/data/')
          .map(r => r.unregister()),
      ))
      .then(() => caches.delete('vigilant-mapdata-v1'))
      .catch(() => { /* non-fatal */ });
  });
}
