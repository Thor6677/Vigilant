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
