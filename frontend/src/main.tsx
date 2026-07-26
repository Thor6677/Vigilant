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

// Register the star-map service worker to cache the static system/edge/region
// JSON bundles for instant load on revisits. Only on production-like hosts
// (HTTPS required by the API). Silently no-ops if unsupported.
if ('serviceWorker' in navigator && location.protocol === 'https:') {
  window.addEventListener('load', () => {
    // Scope limited to /map/data/ — that's the only thing we cache, and it
    // matches the SW file's location (cannot claim a broader scope without
    // a Service-Worker-Allowed response header).
    navigator.serviceWorker.register('/map/data/map-sw.js', { scope: '/map/data/' })
      .catch(() => { /* non-fatal */ });
  });
}
