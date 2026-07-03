import { AmbientBackground, Panel } from '@vigilant/ui';

// AmbientBackground is a live canvas: it fetches real New Eden system
// coordinates + ESI sovereignty data at runtime and renders a camera
// flythrough with kill blips. A static capture cannot show that truthfully,
// so this preview mounts the real component (its canvas is present behind
// the panel) with an honest description of what renders at runtime.
export const LiveCanvas = () => (
  <div style={{ position: 'relative', minHeight: '220px' }}>
    <AmbientBackground minWidth={0} />
    <div style={{ position: 'relative', display: 'flex', justifyContent: 'center', paddingTop: '40px' }}>
      <Panel title="Ambient Background" glass brackets>
        <div className="b-pad-md" style={{ maxWidth: '420px' }}>
          <span className="b-muted-sm">
            LIVE CANVAS — renders the New Eden star map flythrough at runtime:
            5,485 real systems, live sovereignty colors from ESI, red kill
            blips. Use it on the SSO login screen only; it sits behind content
            at z-index −1.
          </span>
        </div>
      </Panel>
    </div>
  </div>
);
