import type { SystemData, RoutePreference } from '../types';
import { securityColorCSS } from '../utils/colors';

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
  // Clamp panel position to viewport
  const left = Math.min(position.x + 20, window.innerWidth - 300);
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
        width: 270,
        background: 'rgba(10, 14, 30, 0.95)',
        border: '1px solid #2a3a5a',
        borderRadius: 8,
        padding: '14px 16px',
        fontFamily: 'DM Sans, sans-serif',
        color: '#C8D8E8',
        zIndex: 20,
        boxShadow: '0 4px 24px rgba(0,0,0,0.5)',
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <h3 style={{
          margin: 0, fontFamily: 'Rajdhani, sans-serif', fontWeight: 600, fontSize: 18,
          color: '#e0eaf4',
        }}>
          {system.name}
        </h3>
        <button
          onClick={onClose}
          style={{
            background: 'none', border: 'none', color: '#6a7a8a',
            cursor: 'pointer', fontSize: 18, lineHeight: 1, padding: '0 2px',
          }}
        >
          &times;
        </button>
      </div>

      {/* Details */}
      <div style={{ fontSize: 13, lineHeight: 1.6 }}>
        <div>
          <span style={{ color: '#6a7a8a' }}>Security: </span>
          <span style={{ color: securityColorCSS(system.sec), fontWeight: 700 }}>
            {system.sec.toFixed(1)}
          </span>
        </div>
        <div>
          <span style={{ color: '#6a7a8a' }}>Region: </span>
          {system.regName}
        </div>
        <div>
          <span style={{ color: '#6a7a8a' }}>Constellation: </span>
          {system.conName}
        </div>
        {system.hasStation && (
          <div style={{ color: '#48f148', marginTop: 2 }}>NPC Station</div>
        )}
      </div>

      {/* Route info */}
      {activeRoute && activeRoute.length > 1 && (isOrigin || isDest) && (
        <div style={{
          marginTop: 10, padding: '6px 8px', background: 'rgba(0, 212, 255, 0.08)',
          borderRadius: 4, fontSize: 12, color: '#00d4ff',
        }}>
          Route: {jumpCount} jump{jumpCount !== 1 ? 's' : ''}
        </div>
      )}

      {/* Route preference */}
      {(routeOrigin !== null || routeDest !== null) && (
        <div style={{ marginTop: 8 }}>
          <label style={{ fontSize: 11, color: '#6a7a8a', display: 'block', marginBottom: 4 }}>
            Route Preference:
          </label>
          <select
            value={routePreference}
            onChange={(e) => onSetRoutePreference(e.target.value as RoutePreference)}
            style={{
              width: '100%', padding: '4px 6px', fontSize: 12,
              background: '#141828', color: '#C8D8E8', border: '1px solid #2a3a5a',
              borderRadius: 4, cursor: 'pointer',
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
      <div style={{ marginTop: 12, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        <ActionButton
          label={isOrigin ? 'Origin' : 'Set Origin'}
          active={isOrigin}
          onClick={() => onSetOrigin(system.id)}
        />
        <ActionButton
          label={isDest ? 'Destination' : 'Set Dest'}
          active={isDest}
          onClick={() => onSetDestination(system.id)}
        />
      </div>

      {/* External links */}
      <div style={{ marginTop: 10, display: 'flex', gap: 10, fontSize: 12 }}>
        <a
          href={`https://evemaps.dotlan.net/system/${system.name}`}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: '#5a8ab5', textDecoration: 'none' }}
        >
          DOTLAN
        </a>
        <a
          href={`https://zkillboard.com/system/${system.id}/`}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: '#5a8ab5', textDecoration: 'none' }}
        >
          zKillboard
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
        padding: '5px 12px',
        fontSize: 12,
        fontFamily: 'DM Sans, sans-serif',
        background: active ? 'rgba(0, 212, 255, 0.15)' : 'rgba(42, 58, 90, 0.4)',
        color: active ? '#00d4ff' : '#8a9ab0',
        border: `1px solid ${active ? '#00d4ff' : '#2a3a5a'}`,
        borderRadius: 4,
        cursor: 'pointer',
      }}
    >
      {label}
    </button>
  );
}
