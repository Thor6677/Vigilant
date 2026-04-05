import type { SystemData, RoutePreference } from '../types';
import { securityColorCSS } from '../utils/colors';

// Vigilant design tokens
const FONT = "'JetBrains Mono', monospace";
const BG = 'rgba(14, 14, 14, 0.97)';
const BORDER = '#191919';
const TEXT = '#dedede';
const MUTED = '#474747';
const ACCENT = '#c8a951';

interface Props {
  system: SystemData;
  position: { x: number; y: number };
  routeOrigin: number | null;
  routeDest: number | null;
  activeRoute: number[] | null;
  routePreference: RoutePreference;
  onSetOrigin: (id: number) => void;
  onSetDestination: (id: number) => void;
  onSetRoutePreference: (pref: RoutePreference) => void;
  onClose: () => void;
}

export function SystemInfoPanel({
  system,
  position,
  routeOrigin,
  routeDest,
  activeRoute,
  routePreference,
  onSetOrigin,
  onSetDestination,
  onSetRoutePreference,
  onClose,
}: Props) {
  const left = Math.min(position.x + 20, window.innerWidth - 280);
  const top = Math.min(Math.max(position.y - 60, 10), window.innerHeight - 350);

  const isOrigin = routeOrigin === system.id;
  const isDest = routeDest === system.id;
  const jumpCount = activeRoute ? activeRoute.length - 1 : null;

  return (
    <div
      style={{
        position: 'absolute',
        left,
        top,
        width: 260,
        background: BG,
        border: `1px solid ${BORDER}`,
        padding: '12px 14px',
        fontFamily: FONT,
        fontSize: 11,
        color: TEXT,
        zIndex: 20,
        boxShadow: '0 4px 20px rgba(0,0,0,0.6)',
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 600, letterSpacing: '0.05em', color: TEXT }}>
          {system.name}
        </span>
        <button
          onClick={onClose}
          style={{
            background: 'none', border: 'none', color: MUTED,
            cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: '0 2px',
            fontFamily: FONT,
          }}
        >
          ×
        </button>
      </div>

      {/* Details */}
      <div style={{ lineHeight: 1.8 }}>
        <div>
          <span style={{ color: MUTED }}>SEC </span>
          <span style={{ color: securityColorCSS(system.sec), fontWeight: 600 }}>
            {system.sec.toFixed(1)}
          </span>
        </div>
        <div><span style={{ color: MUTED }}>RGN </span>{system.regName}</div>
        <div><span style={{ color: MUTED }}>CON </span>{system.conName}</div>
        {system.hasStation && (
          <div style={{ color: '#33aa55', marginTop: 2 }}>NPC STATION</div>
        )}
      </div>

      {/* Route info */}
      {activeRoute && activeRoute.length > 1 && (isOrigin || isDest) && (
        <div style={{
          marginTop: 8, padding: '4px 8px', background: 'rgba(200,169,81,0.08)',
          fontSize: 10, color: ACCENT, border: `1px solid rgba(200,169,81,0.2)`,
        }}>
          ROUTE: {jumpCount} JUMP{jumpCount !== 1 ? 'S' : ''}
        </div>
      )}

      {/* Route preference */}
      {(routeOrigin !== null || routeDest !== null) && (
        <div style={{ marginTop: 8 }}>
          <label style={{ fontSize: 9, color: MUTED, display: 'block', marginBottom: 3, letterSpacing: '0.12em', textTransform: 'uppercase' }}>
            Route Preference
          </label>
          <select
            value={routePreference}
            onChange={(e) => onSetRoutePreference(e.target.value as RoutePreference)}
            style={{
              width: '100%', padding: '3px 4px', fontSize: 10,
              fontFamily: FONT,
              background: '#080808', color: TEXT, border: `1px solid ${BORDER}`,
              cursor: 'pointer',
            }}
          >
            <option value="shortest">Shortest</option>
            <option value="highsec">Prefer Highsec</option>
            <option value="lowsec">Prefer Lowsec</option>
            <option value="nullsec">Prefer Nullsec</option>
          </select>
        </div>
      )}

      {/* Action buttons */}
      <div style={{ marginTop: 10, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        <ActionButton
          label={isOrigin ? '● ORIGIN' : 'SET ORIGIN'}
          active={isOrigin}
          onClick={() => onSetOrigin(system.id)}
        />
        <ActionButton
          label={isDest ? '● DEST' : 'SET DEST'}
          active={isDest}
          onClick={() => onSetDestination(system.id)}
        />
      </div>

      {/* External links */}
      <div style={{ marginTop: 8, display: 'flex', gap: 12, fontSize: 10 }}>
        <a
          href={`https://evemaps.dotlan.net/system/${system.name}`}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: MUTED, textDecoration: 'none', letterSpacing: '0.08em' }}
        >
          DOTLAN ↗
        </a>
        <a
          href={`https://zkillboard.com/system/${system.id}/`}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: MUTED, textDecoration: 'none', letterSpacing: '0.08em' }}
        >
          ZKILL ↗
        </a>
      </div>
    </div>
  );
}

function ActionButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '4px 10px',
        fontSize: 9,
        letterSpacing: '0.1em',
        fontFamily: FONT,
        background: active ? 'rgba(200,169,81,0.1)' : 'transparent',
        color: active ? ACCENT : MUTED,
        border: `1px solid ${active ? ACCENT : BORDER}`,
        cursor: 'pointer',
      }}
    >
      {label}
    </button>
  );
}
