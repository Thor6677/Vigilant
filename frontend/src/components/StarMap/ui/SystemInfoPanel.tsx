import type { SystemData, RoutePreference } from '../types';
import type { MapStats } from '../useOverlayData';
import { securityColorCSS } from '../utils/colors';

const FONT = "'JetBrains Mono', monospace";
const BG = 'rgba(14, 14, 14, 0.97)';
const BORDER = '#191919';
const TEXT = '#dedede';
const MUTED = '#474747';
const ACCENT = '#c8a951';

const SVC_LABELS: Record<string, string> = {
  cloning: 'CLONE',
  factory: 'MFG',
  lab: 'LAB',
  market: 'MKT',
  refinery: 'REF',
  repair: 'RPR',
  reprocessing: 'REPR',
  jumpClone: 'JC',
};

interface Props {
  system: SystemData;
  position: { x: number; y: number };
  stats: MapStats | null;
  routeOrigin: number | null;
  routeDest: number | null;
  activeRoute: number[] | null;
  routePreference: RoutePreference;
  onSetOrigin: (id: number) => void;
  onSetDestination: (id: number) => void;
  onSetRoutePreference: (pref: RoutePreference) => void;
  onClose: () => void;
  onSetJumpOrigin?: (id: number) => void;
  onSetJumpDest?: (id: number) => void;
}

export function SystemInfoPanel({
  system,
  position,
  stats,
  routeOrigin,
  routeDest,
  activeRoute,
  routePreference,
  onSetOrigin,
  onSetDestination,
  onSetRoutePreference,
  onClose,
  onSetJumpOrigin,
  onSetJumpDest,
}: Props) {
  const left = Math.min(position.x + 20, window.innerWidth - 280);
  const top = Math.min(Math.max(position.y - 60, 10), window.innerHeight - 400);

  const isOrigin = routeOrigin === system.id;
  const isDest = routeDest === system.id;
  const jumpCount = activeRoute ? activeRoute.length - 1 : null;

  // Get stats for this system
  const sid = String(system.id);
  const kills = stats?.kills[sid];
  const jumps = stats?.jumps[sid];
  const sov = stats?.sovereignty[sid];
  const hasStats = kills || jumps;

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
          <div style={{ marginTop: 3 }}>
            <span style={{ color: '#33aa55' }}>
              {system.stns} NPC STATION{system.stns > 1 ? 'S' : ''}
            </span>
          </div>
        )}
        {system.svcs.length > 0 && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3, marginTop: 3 }}>
            {system.svcs.map(svc => (
              <span key={svc} style={{
                fontSize: 7, padding: '1px 4px', letterSpacing: '0.08em',
                background: 'rgba(51, 170, 85, 0.08)',
                color: '#33aa55', border: '1px solid rgba(51, 170, 85, 0.15)',
              }}>
                {SVC_LABELS[svc] ?? svc.toUpperCase()}
              </span>
            ))}
          </div>
        )}
        {sov?.alliance_id && (
          <div style={{ marginTop: 2 }}>
            <span style={{ color: MUTED }}>SOV </span>
            <span style={{ color: '#6688aa' }}>Alliance {sov.alliance_id}</span>
          </div>
        )}
      </div>

      {/* Activity stats */}
      {hasStats && (
        <div style={{
          marginTop: 8, padding: '6px 8px', background: '#0a0a0a',
          border: `1px solid ${BORDER}`, fontSize: 9, letterSpacing: '0.1em',
          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '3px 12px',
        }}>
          {jumps !== undefined && jumps > 0 && (
            <>
              <span style={{ color: MUTED }}>JUMPS</span>
              <span style={{ textAlign: 'right' }}>{jumps.toLocaleString()}</span>
            </>
          )}
          {kills?.ship !== undefined && kills.ship > 0 && (
            <>
              <span style={{ color: '#cc3333' }}>SHIP KILLS</span>
              <span style={{ textAlign: 'right', color: '#cc3333' }}>{kills.ship.toLocaleString()}</span>
            </>
          )}
          {kills?.npc !== undefined && kills.npc > 0 && (
            <>
              <span style={{ color: MUTED }}>NPC KILLS</span>
              <span style={{ textAlign: 'right' }}>{kills.npc.toLocaleString()}</span>
            </>
          )}
          {kills?.pod !== undefined && kills.pod > 0 && (
            <>
              <span style={{ color: '#cc3333' }}>POD KILLS</span>
              <span style={{ textAlign: 'right', color: '#cc3333' }}>{kills.pod.toLocaleString()}</span>
            </>
          )}
        </div>
      )}

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

      {/* Gate route buttons */}
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

      {/* Jump planner buttons — always visible */}
      {onSetJumpOrigin && onSetJumpDest && (
        <div style={{ marginTop: 6, display: 'flex', gap: 4, flexWrap: 'wrap' }}>
          <button
            onClick={() => onSetJumpOrigin(system.id)}
            style={{
              padding: '4px 10px', fontSize: 9, letterSpacing: '0.1em', fontFamily: FONT,
              background: 'rgba(255,136,0,0.08)', color: '#ff8800',
              border: '1px solid rgba(255,136,0,0.2)', cursor: 'pointer',
            }}
          >
            JUMP ORIGIN
          </button>
          <button
            onClick={() => onSetJumpDest(system.id)}
            style={{
              padding: '4px 10px', fontSize: 9, letterSpacing: '0.1em', fontFamily: FONT,
              background: 'rgba(255,136,0,0.08)', color: '#ff8800',
              border: '1px solid rgba(255,136,0,0.2)', cursor: 'pointer',
            }}
          >
            JUMP DEST
          </button>
        </div>
      )}

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
