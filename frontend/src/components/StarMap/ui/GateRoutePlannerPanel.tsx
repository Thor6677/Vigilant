import { useState } from 'react';
import type { SystemData, RoutePreference } from '../types';
import type { GateRoutePlannerState, AvoidEntry, SavedRoute } from '../useGateRoutePlanner';
import type { CharacterLocation } from '../useCharacterLocations';
import { securityColorCSS } from '../utils/colors';
import { SystemSlotWithSearch } from './SystemSlotWithSearch';
import { FONT, BG, BORDER, TEXT, MUTED, GATE_COLOR } from './plannerStyles';

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
          <div style={{ fontSize: 9, color: MUTED, letterSpacing: '0.1em', marginBottom: 6 }}>
            ROUTE: {jumpCount} JUMP{jumpCount !== 1 ? 'S' : ''}
          </div>
          <RouteStopList
            stops={stops}
            systems={systems}
            systemMap={systemMap}
            onReorderWaypoint={planner.reorderWaypoint}
            onInsertWaypointAt={planner.insertWaypointAt}
            onRemoveWaypoint={planner.removeWaypoint}
            onFocusSystem={onFocusSystem}
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

/* ── Route stop list with drag-reorder + insert + remove ─────────── */

function RouteStopList({
  stops,
  systems,
  systemMap,
  onReorderWaypoint,
  onInsertWaypointAt,
  onRemoveWaypoint,
  onFocusSystem,
}: {
  stops: number[];
  systems: SystemData[];
  systemMap: Map<number, SystemData>;
  onReorderWaypoint: (from: number, to: number) => void;
  onInsertWaypointAt: (index: number, systemId: number) => void;
  onRemoveWaypoint: (id: number) => void;
  onFocusSystem: (system: SystemData) => void;
}) {
  const [draggingWaypointIdx, setDraggingWaypointIdx] = useState<number | null>(null);
  const [dragOverWaypointIdx, setDragOverWaypointIdx] = useState<number | null>(null);
  const [insertAfterStopIdx, setInsertAfterStopIdx] = useState<number | null>(null);

  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      {stops.map((id, i) => {
        const sys = systemMap.get(id);
        if (!sys) return null;
        const isOrigin = i === 0;
        const isDest = i === stops.length - 1;
        const isWaypoint = !isOrigin && !isDest;
        // Index in the waypoints[] array (origin at i=0, waypoints start at i=1)
        const waypointIndex = i - 1;
        const isDragSource = isWaypoint && draggingWaypointIdx === waypointIndex;
        const isDropTarget = isWaypoint && dragOverWaypointIdx === waypointIndex && draggingWaypointIdx !== null && draggingWaypointIdx !== waypointIndex;

        return (
          <div key={`${id}-${i}`}>
            <div
              draggable={isWaypoint}
              onDragStart={isWaypoint ? (e) => {
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', String(waypointIndex));
                setDraggingWaypointIdx(waypointIndex);
              } : undefined}
              onDragEnd={isWaypoint ? () => {
                setDraggingWaypointIdx(null);
                setDragOverWaypointIdx(null);
              } : undefined}
              onDragOver={isWaypoint ? (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (waypointIndex !== draggingWaypointIdx) {
                  setDragOverWaypointIdx(waypointIndex);
                }
              } : undefined}
              onDragLeave={isWaypoint ? () => {
                if (dragOverWaypointIdx === waypointIndex) {
                  setDragOverWaypointIdx(null);
                }
              } : undefined}
              onDrop={isWaypoint ? (e) => {
                e.preventDefault();
                const fromIdx = Number(e.dataTransfer.getData('text/plain'));
                if (!Number.isNaN(fromIdx) && fromIdx !== waypointIndex) {
                  onReorderWaypoint(fromIdx, waypointIndex);
                }
                setDraggingWaypointIdx(null);
                setDragOverWaypointIdx(null);
              } : undefined}
              style={{
                padding: '4px 0',
                borderTop: i > 0 ? `1px solid ${BORDER}` : 'none',
                borderBottom: isDropTarget ? `1px solid ${GATE_COLOR}` : undefined,
                fontSize: 9,
                display: 'flex', alignItems: 'center', gap: 4,
                opacity: isDragSource ? 0.4 : 1,
                cursor: isWaypoint ? 'grab' : 'default',
              }}
            >
              <span
                onClick={() => onFocusSystem(sys)}
                style={{
                  color: isOrigin ? '#33aa55' : isDest ? '#cc5533' : TEXT,
                  cursor: 'pointer', flex: 1,
                }}
              >
                {i + 1}. {sys.name}
                <span style={{
                  color: securityColorCSS(sys.sec), marginLeft: 4, fontSize: 8,
                }}>
                  {sys.sec.toFixed(1)}
                </span>
                {sys.hasStation && (
                  <span style={{ color: '#33aa55', marginLeft: 3, fontSize: 7 }}>STN</span>
                )}
              </span>
              {isWaypoint && (
                <MiniBtn title="Remove waypoint" onClick={() => onRemoveWaypoint(id)}>×</MiniBtn>
              )}
            </div>

            {/* Insert button between this stop and the next */}
            {!isDest && (
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
            {insertAfterStopIdx === i && (
              <InsertWaypointSearch
                systems={systems}
                onSelect={(systemId) => {
                  // Insert at waypoints[i] — the new waypoint goes BEFORE the
                  // current waypoints[i], because i is the stops-index of the
                  // stop AFTER which we're inserting (origin = stop 0, so to
                  // insert after origin we want waypoints[0], etc.)
                  onInsertWaypointAt(i, systemId);
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

