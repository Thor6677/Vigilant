import { useState } from 'react';
import type { SystemData, RoutePreference } from '../types';
import type {
  GateRoutePlannerState,
  AvoidEntry,
  SavedRoute,
  HopIntel,
  ThreatLevel,
} from '../useGateRoutePlanner';
import type { CharacterLocation } from '../useCharacterLocations';
import { securityColorCSS } from '../utils/colors';
import { SystemSlotWithSearch } from './SystemSlotWithSearch';
import { FONT, BG, BORDER, TEXT, MUTED, GATE_COLOR } from './plannerStyles';

const THREAT_COLORS: Record<ThreatLevel, string> = {
  safe: '#33aa55',
  caution: '#cc8844',
  dangerous: '#cc3333',
  smartbomb: '#cc33cc',
};

interface Props {
  planner: GateRoutePlannerState;
  systems: SystemData[];
  systemMap: Map<number, SystemData>;
  systemName: (id: number) => string;
  characters: CharacterLocation[];
  onFocusSystem: (system: SystemData) => void;
}

const PREFERENCE_OPTIONS: { value: RoutePreference; label: string }[] = [
  { value: 'shortest', label: 'Shortest' },
  { value: 'safest', label: 'Safest (kill data)' },
  { value: 'highsec', label: 'Prefer Highsec' },
  { value: 'lowsec', label: 'Prefer Lowsec' },
  { value: 'nullsec', label: 'Prefer Nullsec' },
];

export function GateRoutePlannerPanel({
  planner,
  systems,
  systemMap,
  systemName,
  characters,
  onFocusSystem,
}: Props) {
  const charsWithLocation = characters.filter(c => c.system_id !== null);
  const [savingMode, setSavingMode] = useState(false);
  const [saveName, setSaveName] = useState('');
  const [copiedToken, setCopiedToken] = useState<string | null>(null);

  const stops: number[] = planner.origin !== null && planner.dest !== null
    ? [planner.origin, ...planner.waypoints, planner.dest]
    : [];
  const jumpCount = planner.activeRoute ? planner.activeRoute.length - 1 : null;

  return (
    <div style={{
      position: 'absolute',
      top: 48,
      right: 10,
      width: 280,
      maxHeight: 'calc(100% - 100px)',
      overflowY: 'auto',
      background: BG,
      border: `1px solid ${BORDER}`,
      padding: '10px 12px',
      fontFamily: FONT,
      fontSize: 10,
      color: TEXT,
      zIndex: 30,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.1em', color: GATE_COLOR }}>
          GATE ROUTE
        </span>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <button onClick={planner.reset} title="Reset" style={{
            background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
            fontSize: 9, fontFamily: FONT, letterSpacing: '0.08em',
          }}>RESET</button>
          <button onClick={() => planner.setActive(false)} style={{
            background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
            fontSize: 14, fontFamily: FONT, lineHeight: 1,
          }}>×</button>
        </div>
      </div>

      {/* Origin */}
      <Label text="ORIGIN" />
      <SystemSlotWithSearch
        systemId={planner.origin}
        systems={systems}
        systemName={systemName}
        characters={charsWithLocation}
        onSelect={(id) => planner.setOrigin(id)}
        onClear={() => planner.setOrigin(null)}
        onFocusSystem={onFocusSystem}
        placeholder="Search or click map..."
        isOrigin
      />

      {/* Swap button */}
      <div style={{ display: 'flex', justifyContent: 'center', padding: '2px 0' }}>
        <button
          onClick={planner.swapEndpoints}
          disabled={planner.origin === null && planner.dest === null}
          title="Swap origin and destination"
          style={{
            background: 'none', border: `1px solid ${BORDER}`, color: MUTED,
            cursor: 'pointer', fontSize: 9, fontFamily: FONT, padding: '1px 8px',
          }}
        >
          ↕
        </button>
      </div>

      {/* Destination */}
      <Label text="DESTINATION" />
      <SystemSlotWithSearch
        systemId={planner.dest}
        systems={systems}
        systemName={systemName}
        characters={charsWithLocation}
        onSelect={(id) => planner.setDest(id)}
        onClear={() => planner.setDest(null)}
        onFocusSystem={onFocusSystem}
        placeholder="Search or click map..."
      />

      {/* Preference dropdown */}
      <div style={{ marginTop: 8 }}>
        <Label text="PREFERENCE" />
        <select
          value={planner.preference}
          onChange={e => planner.setPreference(e.target.value as RoutePreference)}
          style={{
            width: '100%', padding: '4px 6px', fontSize: 10, fontFamily: FONT,
            background: '#080808', color: TEXT, border: `1px solid ${BORDER}`,
            cursor: 'pointer',
          }}
        >
          {PREFERENCE_OPTIONS.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>

      {/* Error message */}
      {planner.errorMessage && (
        <div style={{
          fontSize: 9, color: '#cc3333', marginTop: 8, padding: '4px 6px',
          background: 'rgba(204, 51, 51, 0.08)', border: '1px solid #441818',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <span>{planner.errorMessage}</span>
          <button onClick={planner.clearError} style={{
            background: 'none', border: 'none', color: '#cc3333', cursor: 'pointer',
            fontSize: 12, fontFamily: FONT, lineHeight: 1, padding: '0 2px',
          }}>×</button>
        </div>
      )}

      {/* Route results */}
      {planner.activeRoute && jumpCount !== null && jumpCount >= 1 && (
        <div style={{ marginTop: 10 }}>
          <div style={{
            fontSize: 9, color: MUTED, letterSpacing: '0.1em', marginBottom: 6,
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          }}>
            <span>ROUTE: {jumpCount} JUMP{jumpCount !== 1 ? 'S' : ''}</span>
            {planner.hopIntelLoading && (
              <span style={{ fontSize: 8, color: '#3a3a3a' }}>checking intel…</span>
            )}
          </div>
          <RouteHopList
            route={planner.activeRoute}
            stops={stops}
            waypoints={planner.waypoints}
            systems={systems}
            systemMap={systemMap}
            hopIntel={planner.hopIntel}
            onReorderWaypoint={planner.reorderWaypoint}
            onInsertWaypointAt={planner.insertWaypointAt}
            onRemoveWaypoint={planner.removeWaypoint}
            onFocusSystem={onFocusSystem}
            onAddWaypointToAutopilot={planner.activeCharacterId !== null
              ? planner.pushWaypointToAutopilot
              : undefined}
          />

          {/* Set Destination in EVE + character picker + auto-trim toggle */}
          <AutopilotControls
            planner={planner}
            characters={characters}
          />
        </div>
      )}

      {/* Avoid list */}
      <div style={{ marginTop: 14 }}>
        <Label text={`AVOID LIST [${planner.avoidEntries.length}]`} />
        {planner.avoidEntries.length === 0 ? (
          <div style={{ fontSize: 9, color: '#2a2a2a', padding: '2px 0' }}>
            Click <span style={{ color: '#cc3333' }}>×</span> on a system in search results to add.
          </div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
            {planner.avoidEntries.map(entry => (
              <AvoidChip
                key={entry.id}
                entry={entry}
                systemMap={systemMap}
                onRemove={() => planner.removeAvoid(entry.id)}
              />
            ))}
            {planner.avoidEntries.length > 1 && (
              <button
                onClick={() => planner.clearAvoid()}
                style={{
                  background: 'none', border: `1px solid ${BORDER}`, color: MUTED,
                  cursor: 'pointer', fontSize: 8, fontFamily: FONT, padding: '2px 6px',
                  letterSpacing: '0.08em',
                }}
              >
                CLEAR ALL
              </button>
            )}
          </div>
        )}
      </div>

      {/* Save current route */}
      <div style={{ marginTop: 14 }}>
        <Label text="SAVED ROUTES" />
        {!savingMode ? (
          <button
            onClick={() => setSavingMode(true)}
            disabled={planner.origin === null || planner.dest === null}
            style={{
              width: '100%', padding: '5px', fontSize: 9, letterSpacing: '0.1em',
              fontFamily: FONT, textTransform: 'uppercase',
              background: planner.origin !== null && planner.dest !== null
                ? 'rgba(0,212,255,0.10)' : 'transparent',
              color: planner.origin !== null && planner.dest !== null ? GATE_COLOR : MUTED,
              border: `1px solid ${planner.origin !== null && planner.dest !== null ? GATE_COLOR : BORDER}`,
              cursor: planner.origin !== null && planner.dest !== null ? 'pointer' : 'default',
              marginBottom: 6,
            }}
          >
            Save Current Route
          </button>
        ) : (
          <div style={{ display: 'flex', gap: 3, marginBottom: 6 }}>
            <input
              type="text"
              value={saveName}
              onChange={e => setSaveName(e.target.value)}
              placeholder="Route name..."
              autoFocus
              maxLength={64}
              onKeyDown={async e => {
                if (e.key === 'Enter' && saveName.trim()) {
                  await planner.saveCurrentRoute(saveName.trim());
                  setSaveName('');
                  setSavingMode(false);
                } else if (e.key === 'Escape') {
                  setSaveName('');
                  setSavingMode(false);
                }
              }}
              style={{
                flex: 1, padding: '4px 6px', fontSize: 9, fontFamily: FONT,
                background: '#080808', color: TEXT, border: `1px solid ${BORDER}`,
                outline: 'none',
              }}
            />
            <button
              onClick={async () => {
                if (saveName.trim()) {
                  await planner.saveCurrentRoute(saveName.trim());
                  setSaveName('');
                  setSavingMode(false);
                }
              }}
              disabled={!saveName.trim()}
              style={{
                background: 'rgba(0,212,255,0.15)', border: `1px solid ${GATE_COLOR}`,
                color: GATE_COLOR, cursor: saveName.trim() ? 'pointer' : 'default',
                fontSize: 9, fontFamily: FONT, padding: '0 8px',
              }}
            >
              ✓
            </button>
            <button
              onClick={() => { setSaveName(''); setSavingMode(false); }}
              style={{
                background: 'none', border: `1px solid ${BORDER}`, color: MUTED,
                cursor: 'pointer', fontSize: 9, fontFamily: FONT, padding: '0 8px',
              }}
            >
              ×
            </button>
          </div>
        )}

        {planner.savedRoutes.length === 0 ? (
          <div style={{ fontSize: 9, color: '#2a2a2a', padding: '2px 0' }}>
            No saved routes yet.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {planner.savedRoutes.map(route => (
              <SavedRouteRow
                key={route.id}
                route={route}
                onLoad={() => planner.loadSavedRoute(route.id)}
                onDelete={() => planner.deleteSavedRoute(route.id)}
                onToggleShare={async () => {
                  const updated = await planner.toggleShareSavedRoute(route.id);
                  if (updated?.share_token) {
                    const url = `${window.location.origin}/map?route=${updated.share_token}`;
                    try {
                      await navigator.clipboard.writeText(url);
                      setCopiedToken(updated.share_token);
                      window.setTimeout(() => setCopiedToken(null), 2000);
                    } catch {
                      // Clipboard not available — silently skip
                    }
                  }
                }}
                copied={copiedToken === route.share_token && route.share_token !== null}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Route hop list with intel + drag-reorder + insert + remove ─── */

function RouteHopList({
  route,
  stops,
  waypoints,
  systems,
  systemMap,
  hopIntel,
  onReorderWaypoint,
  onInsertWaypointAt,
  onRemoveWaypoint,
  onFocusSystem,
  onAddWaypointToAutopilot,
}: {
  /** Full pathfinding result — every gate hop. */
  route: number[];
  /** User-set stops in order: [origin, ...waypoints, dest]. */
  stops: number[];
  /** Just the waypoint system IDs (origin / dest excluded). */
  waypoints: number[];
  systems: SystemData[];
  systemMap: Map<number, SystemData>;
  hopIntel: Map<number, HopIntel>;
  onReorderWaypoint: (from: number, to: number) => void;
  onInsertWaypointAt: (index: number, systemId: number) => void;
  onRemoveWaypoint: (id: number) => void;
  onFocusSystem: (system: SystemData) => void;
  onAddWaypointToAutopilot?: (systemId: number) => Promise<string | null>;
}) {
  const [draggingWaypointIdx, setDraggingWaypointIdx] = useState<number | null>(null);
  const [dragOverWaypointIdx, setDragOverWaypointIdx] = useState<number | null>(null);
  const [insertAfterStopIdx, setInsertAfterStopIdx] = useState<number | null>(null);
  const [expandedHop, setExpandedHop] = useState<number | null>(null);

  // Pre-compute classification for every system in the route:
  // is it origin / a user waypoint / dest / intermediate?
  // We also track the user-stop ordinal (1-based) so the user can see
  // "stop 3" vs every-hop count.
  const originId = stops[0];
  const destId = stops[stops.length - 1];
  const waypointSet = new Set(waypoints);
  // Map from waypoint system_id → its index in waypoints[] (for drag-reorder)
  const waypointIndexById = new Map<number, number>();
  waypoints.forEach((id, idx) => waypointIndexById.set(id, idx));

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {route.map((id, i) => {
        const sys = systemMap.get(id);
        if (!sys) return null;
        const isOrigin = id === originId && i === 0;
        const isDest = id === destId && i === route.length - 1;
        const isUserWaypoint = !isOrigin && !isDest && waypointSet.has(id);
        const isIntermediate = !isOrigin && !isDest && !isUserWaypoint;

        // For drag-reorder, the row only acts as draggable if it's a user
        // waypoint. We use the waypoints[] index, not the route index.
        const waypointIndex = isUserWaypoint ? (waypointIndexById.get(id) ?? -1) : -1;
        const isDragSource = isUserWaypoint && draggingWaypointIdx === waypointIndex;
        const isDropTarget = isUserWaypoint
          && dragOverWaypointIdx === waypointIndex
          && draggingWaypointIdx !== null
          && draggingWaypointIdx !== waypointIndex;

        // Insert-button between hops only fires AFTER user stops (origin or
        // a waypoint). Inserting after an intermediate hop doesn't make sense
        // because intermediates are computed, not user-set.
        const isStopBeforeAnother = (isOrigin || isUserWaypoint) && !isDest;
        // The waypoints[] index where the new waypoint should land if the
        // user clicks "+" after this row.
        const insertAtWaypointIdx = isOrigin
          ? 0
          : isUserWaypoint ? waypointIndex + 1 : -1;

        const intel = hopIntel.get(id);
        const threatColor = intel ? THREAT_COLORS[intel.threat] : null;

        return (
          <div key={`${id}-${i}`}>
            <div
              draggable={isUserWaypoint}
              onDragStart={isUserWaypoint ? (e) => {
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', String(waypointIndex));
                setDraggingWaypointIdx(waypointIndex);
              } : undefined}
              onDragEnd={isUserWaypoint ? () => {
                setDraggingWaypointIdx(null);
                setDragOverWaypointIdx(null);
              } : undefined}
              onDragOver={isUserWaypoint ? (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (waypointIndex !== draggingWaypointIdx) {
                  setDragOverWaypointIdx(waypointIndex);
                }
              } : undefined}
              onDragLeave={isUserWaypoint ? () => {
                if (dragOverWaypointIdx === waypointIndex) {
                  setDragOverWaypointIdx(null);
                }
              } : undefined}
              onDrop={isUserWaypoint ? (e) => {
                e.preventDefault();
                const fromIdx = Number(e.dataTransfer.getData('text/plain'));
                if (!Number.isNaN(fromIdx) && fromIdx !== waypointIndex) {
                  onReorderWaypoint(fromIdx, waypointIndex);
                }
                setDraggingWaypointIdx(null);
                setDragOverWaypointIdx(null);
              } : undefined}
              style={{
                padding: '3px 0',
                borderTop: i > 0 ? `1px solid ${BORDER}` : 'none',
                borderBottom: isDropTarget ? `1px solid ${GATE_COLOR}` : undefined,
                fontSize: 9,
                display: 'flex', alignItems: 'center', gap: 4,
                opacity: isDragSource ? 0.4 : 1,
                cursor: isUserWaypoint ? 'grab' : 'default',
              }}
            >
              {/* Threat dot */}
              <span
                title={intel ? `${intel.threat} — ${intel.kills} kills last hour` : 'no intel'}
                style={{
                  width: 6, height: 6, borderRadius: '50%' as const,
                  background: threatColor ?? '#1a1a1a',
                  flexShrink: 0, marginLeft: 1,
                }}
              />

              {/* System name + sec + stop role */}
              <span
                onClick={() => onFocusSystem(sys)}
                style={{
                  color: isOrigin ? '#33aa55'
                    : isDest ? '#cc5533'
                    : isUserWaypoint ? GATE_COLOR
                    : isIntermediate ? '#5a5a5a'
                    : TEXT,
                  cursor: 'pointer', flex: 1,
                  fontSize: isIntermediate ? 8 : 9,
                }}
              >
                {i + 1}. {sys.name}
                <span style={{
                  color: securityColorCSS(sys.sec), marginLeft: 4, fontSize: 8,
                }}>
                  {sys.sec.toFixed(1)}
                </span>
                {sys.hasStation && !isIntermediate && (
                  <span style={{ color: '#33aa55', marginLeft: 3, fontSize: 7 }}>STN</span>
                )}
              </span>

              {/* Intel badges */}
              {intel && intel.kills > 0 && (
                <button
                  onClick={() => setExpandedHop(expandedHop === i ? null : i)}
                  title={`${intel.kills} kills (${intel.pvp_kills} PvP) — click for details`}
                  style={{
                    background: 'none', border: `1px solid ${threatColor || BORDER}`,
                    color: threatColor || MUTED,
                    fontSize: 7, fontFamily: FONT, padding: '0 3px',
                    cursor: 'pointer', letterSpacing: '0.05em',
                  }}
                >
                  {intel.kills}K
                </button>
              )}
              {intel?.has_smartbombs && (
                <span title="Smartbombs detected!" style={{
                  fontSize: 7, color: '#cc33cc', fontWeight: 'bold',
                  border: '1px solid #cc33cc', padding: '0 2px',
                }}>SB</span>
              )}
              {intel?.has_hics && (
                <span title="Heavy interdictor!" style={{
                  fontSize: 7, color: '#cc3333', fontWeight: 'bold',
                  border: '1px solid #cc3333', padding: '0 2px',
                }}>HIC</span>
              )}
              {intel?.has_dictors && (
                <span title="Interdictor!" style={{
                  fontSize: 7, color: '#cc3333', fontWeight: 'bold',
                  border: '1px solid #cc3333', padding: '0 2px',
                }}>DIC</span>
              )}

              {/* Add to in-game autopilot (if scope available) */}
              {onAddWaypointToAutopilot && (
                <MiniBtn
                  title="Add as waypoint in EVE autopilot"
                  onClick={() => { onAddWaypointToAutopilot(id); }}
                >+EVE</MiniBtn>
              )}

              {/* Remove (waypoints only) */}
              {isUserWaypoint && (
                <MiniBtn title="Remove waypoint" onClick={() => onRemoveWaypoint(id)}>×</MiniBtn>
              )}
            </div>

            {/* Expanded killmail details */}
            {expandedHop === i && intel && intel.top_kills.length > 0 && (
              <div style={{
                padding: '4px 8px 6px', background: '#0a0a0a',
                borderBottom: `1px solid ${BORDER}`, fontSize: 8,
              }}>
                {intel.top_kills.slice(0, 5).map(km => (
                  <div key={km.killmail_id} style={{
                    padding: '2px 0', display: 'flex', justifyContent: 'space-between',
                    gap: 4, color: km.is_npc ? '#3a3a3a' : TEXT,
                  }}>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {km.victim_ship} <span style={{ color: MUTED }}>({km.attacker_count})</span>
                    </span>
                    <span style={{ color: MUTED, whiteSpace: 'nowrap' }}>{km.value_str}</span>
                    <span style={{ color: MUTED, whiteSpace: 'nowrap' }}>{km.time_str}</span>
                  </div>
                ))}
                <a
                  href={`https://zkillboard.com/system/${id}/`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    display: 'block', marginTop: 4, fontSize: 7, color: GATE_COLOR,
                    textDecoration: 'none', letterSpacing: '0.08em',
                  }}
                >
                  VIEW ALL ON ZKILLBOARD ↗
                </a>
              </div>
            )}

            {/* Insert button between this stop and the next */}
            {isStopBeforeAnother && (
              <div style={{ display: 'flex', justifyContent: 'center', padding: '1px 0' }}>
                <button
                  onClick={() => setInsertAfterStopIdx(insertAfterStopIdx === i ? null : i)}
                  title="Insert waypoint here"
                  style={{
                    background: 'none', border: 'none',
                    color: insertAfterStopIdx === i ? GATE_COLOR : '#1a1a1a',
                    cursor: 'pointer', fontSize: 10, fontFamily: FONT, padding: '0 4px',
                  }}
                >
                  +
                </button>
              </div>
            )}

            {/* Inline insert search */}
            {insertAfterStopIdx === i && insertAtWaypointIdx >= 0 && (
              <InsertWaypointSearch
                systems={systems}
                onSelect={(systemId) => {
                  onInsertWaypointAt(insertAtWaypointIdx, systemId);
                  setInsertAfterStopIdx(null);
                }}
                onCancel={() => setInsertAfterStopIdx(null)}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

function InsertWaypointSearch({
  systems,
  onSelect,
  onCancel,
}: {
  systems: SystemData[];
  onSelect: (systemId: number) => void;
  onCancel: () => void;
}) {
  const [query, setQuery] = useState('');

  const results: SystemData[] = (() => {
    if (!query || query.length < 2) return [];
    const q = query.toLowerCase();
    const matches: SystemData[] = [];
    for (const sys of systems) {
      if (sys.name.toLowerCase().includes(q)) {
        matches.push(sys);
        if (matches.length >= 6) break;
      }
    }
    return matches;
  })();

  return (
    <div style={{
      padding: '4px 0', background: '#0a0a0a',
      border: `1px solid ${BORDER}`, marginBottom: 4,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', padding: '2px 6px' }}>
        <input
          type="text"
          value={query}
          autoFocus
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') onCancel();
            if (e.key === 'Enter' && results[0]) onSelect(results[0].id);
          }}
          placeholder="Insert system..."
          style={{
            flex: 1, background: 'none', border: 'none', outline: 'none',
            color: TEXT, fontSize: 9, fontFamily: FONT, letterSpacing: '0.08em',
          }}
        />
        <button
          onClick={onCancel}
          style={{
            background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
            fontSize: 11, fontFamily: FONT, lineHeight: 1,
          }}
        >×</button>
      </div>
      {results.length > 0 && (
        <div style={{ maxHeight: 140, overflowY: 'auto' }}>
          {results.map(sys => (
            <div
              key={sys.id}
              onMouseDown={() => onSelect(sys.id)}
              style={{
                padding: '3px 6px', fontSize: 9, fontFamily: FONT,
                cursor: 'pointer',
                display: 'flex', justifyContent: 'space-between',
                color: TEXT,
              }}
            >
              <span>
                {sys.name}
                {sys.hasStation && (
                  <span style={{ color: '#33aa55', marginLeft: 3, fontSize: 7 }}>STN</span>
                )}
              </span>
              <span style={{ color: securityColorCSS(sys.sec), fontSize: 8 }}>
                {sys.sec.toFixed(1)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ── Avoid chip ──────────────────────────────────────────────────── */

function AvoidChip({
  entry,
  systemMap,
  onRemove,
}: {
  entry: AvoidEntry;
  systemMap: Map<number, SystemData>;
  onRemove: () => void;
}) {
  const sys = entry.kind === 'system' ? systemMap.get(entry.entity_id) : undefined;
  const label = sys ? sys.name : `${entry.kind} ${entry.entity_id}`;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 3,
      padding: '2px 4px 2px 6px', fontSize: 8, fontFamily: FONT,
      background: 'rgba(204, 51, 51, 0.10)', border: '1px solid #441818',
      color: '#dedede', letterSpacing: '0.05em',
    }}>
      {label}
      <button
        onClick={onRemove}
        title="Remove from avoid list"
        style={{
          background: 'none', border: 'none', color: '#cc3333', cursor: 'pointer',
          fontSize: 11, fontFamily: FONT, lineHeight: 1, padding: '0 0 0 2px',
        }}
      >
        ×
      </button>
    </span>
  );
}

/* ── Saved route row ─────────────────────────────────────────────── */

function SavedRouteRow({
  route,
  onLoad,
  onDelete,
  onToggleShare,
  copied,
}: {
  route: SavedRoute;
  onLoad: () => void;
  onDelete: () => void;
  onToggleShare: () => void;
  copied: boolean;
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 3,
      padding: '4px 6px', background: '#0a0a0a', border: `1px solid ${BORDER}`,
      fontSize: 9,
    }}>
      <span
        onClick={onLoad}
        style={{ flex: 1, color: TEXT, cursor: 'pointer', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
        title={`Load route — ${route.waypoints.length} waypoints, ${route.preference}`}
      >
        {route.name}
      </span>
      <button
        onClick={onToggleShare}
        title={route.share_token ? (copied ? 'Copied!' : 'Copy share link / disable') : 'Enable sharing'}
        style={{
          background: 'none', border: `1px solid ${route.share_token ? GATE_COLOR : BORDER}`,
          color: route.share_token ? GATE_COLOR : MUTED, cursor: 'pointer',
          fontSize: 8, fontFamily: FONT, padding: '1px 4px', letterSpacing: '0.05em',
        }}
      >
        {copied ? 'COPIED' : route.share_token ? 'SHARED' : 'SHARE'}
      </button>
      <button
        onClick={onDelete}
        title="Delete saved route"
        style={{
          background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
          fontSize: 11, fontFamily: FONT, lineHeight: 1, padding: '0 2px',
        }}
      >
        ×
      </button>
    </div>
  );
}

/* ── Helpers ─────────────────────────────────────────────────────── */

function Label({ text }: { text: string }) {
  return (
    <div style={{
      fontSize: 8, color: MUTED, letterSpacing: '0.12em',
      textTransform: 'uppercase', marginBottom: 3,
    }}>
      {text}
    </div>
  );
}

function MiniBtn({
  children,
  onClick,
  title,
  disabled,
}: {
  children: string;
  onClick: () => void;
  title: string;
  disabled?: boolean;
}) {
  return (
    <button
      onClick={(e) => { e.stopPropagation(); if (!disabled) onClick(); }}
      title={title}
      disabled={disabled}
      style={{
        background: 'none',
        border: `1px solid ${disabled ? '#0e0e0e' : BORDER}`,
        color: disabled ? '#1a1a1a' : MUTED,
        cursor: disabled ? 'default' : 'pointer',
        fontSize: 9, fontFamily: FONT, padding: '0 3px',
        lineHeight: '14px',
      }}
    >
      {children}
    </button>
  );
}

/* ── Autopilot controls (character picker + Set Destination + follow) ─ */

function AutopilotControls({
  planner,
  characters,
}: {
  planner: GateRoutePlannerState;
  characters: CharacterLocation[];
}) {
  const [pushing, setPushing] = useState(false);
  const [pushResult, setPushResult] = useState<{ kind: 'ok' | 'err'; message: string } | null>(null);
  const charsWithLocation = characters.filter(c => c.system_id !== null);
  const activeChar = charsWithLocation.find(c => c.character_id === planner.activeCharacterId);

  const onClickPush = async () => {
    setPushing(true);
    setPushResult(null);
    const err = await planner.pushRouteToAutopilot();
    setPushing(false);
    if (err) {
      setPushResult({ kind: 'err', message: err });
    } else {
      setPushResult({ kind: 'ok', message: 'Route sent to in-game autopilot' });
      window.setTimeout(() => setPushResult(null), 4000);
    }
  };

  return (
    <div style={{
      marginTop: 8, padding: '6px 0', borderTop: `1px solid ${BORDER}`,
    }}>
      {/* Character picker — dropdown of online characters (those with a
          known current location) */}
      {charsWithLocation.length > 0 ? (
        <div style={{ marginBottom: 6 }}>
          <Label text="ACTIVE CHARACTER" />
          <select
            value={planner.activeCharacterId ?? ''}
            onChange={e => {
              const v = e.target.value;
              planner.setActiveCharacterId(v === '' ? null : Number(v));
            }}
            style={{
              width: '100%', padding: '4px 6px', fontSize: 10, fontFamily: FONT,
              background: '#080808', color: TEXT, border: `1px solid ${BORDER}`,
              cursor: 'pointer',
            }}
          >
            {charsWithLocation.map(ch => (
              <option key={ch.character_id} value={ch.character_id}>
                {ch.character_name} @ {ch.system_name ?? '?'}
                {ch.is_main ? ' ★' : ''}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div style={{ marginBottom: 6, fontSize: 9, color: MUTED }}>
          No characters online (no recent location data).
        </div>
      )}

      {/* Following indicator */}
      {activeChar && (
        <div style={{
          fontSize: 8, color: MUTED, letterSpacing: '0.06em',
          marginBottom: 4, display: 'flex', gap: 4, alignItems: 'center',
        }}>
          <input
            type="checkbox"
            checked={planner.followCharacter}
            onChange={e => planner.setFollowCharacter(e.target.checked)}
            id="follow-char"
            style={{ margin: 0 }}
          />
          <label htmlFor="follow-char" style={{ cursor: 'pointer', flex: 1 }}>
            FOLLOW <span style={{ color: GATE_COLOR }}>{activeChar.character_name}</span> @ {activeChar.system_name ?? '?'}
          </label>
        </div>
      )}

      {/* Set Destination button */}
      <button
        onClick={onClickPush}
        disabled={pushing || planner.activeCharacterId === null || planner.origin === null || planner.dest === null}
        style={{
          width: '100%', padding: '6px', fontSize: 10, letterSpacing: '0.12em',
          fontFamily: FONT, textTransform: 'uppercase',
          background: pushing ? 'transparent' : 'rgba(0,212,255,0.15)',
          color: planner.activeCharacterId !== null && planner.origin !== null && planner.dest !== null
            ? GATE_COLOR : MUTED,
          border: `1px solid ${planner.activeCharacterId !== null && planner.origin !== null && planner.dest !== null
            ? GATE_COLOR : BORDER}`,
          cursor: pushing ? 'default' : 'pointer',
        }}
      >
        {pushing ? 'Sending…' : 'Set Destination in EVE'}
      </button>

      {/* Toast */}
      {pushResult && (
        <div style={{
          marginTop: 4, padding: '4px 6px', fontSize: 8, letterSpacing: '0.06em',
          color: pushResult.kind === 'ok' ? '#33aa55' : '#cc3333',
          background: pushResult.kind === 'ok' ? 'rgba(51,170,85,0.08)' : 'rgba(204,51,51,0.08)',
          border: `1px solid ${pushResult.kind === 'ok' ? '#1d3a25' : '#441818'}`,
          display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 4,
        }}>
          <span style={{ flex: 1 }}>{pushResult.message}</span>
          {pushResult.kind === 'err' && pushResult.message.toLowerCase().includes('re-author') && (
            <a
              href="/auth/add-character"
              style={{ color: GATE_COLOR, textDecoration: 'none', whiteSpace: 'nowrap' }}
            >
              RE-AUTH ↗
            </a>
          )}
          <button onClick={() => setPushResult(null)} style={{
            background: 'none', border: 'none', color: 'inherit', cursor: 'pointer',
            fontSize: 11, lineHeight: 1, padding: '0 2px', fontFamily: FONT,
          }}>×</button>
        </div>
      )}
    </div>
  );
}

