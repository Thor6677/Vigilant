import { useState, useEffect } from 'react';
import type { SystemData, JumpShipClass } from '../types';
import type { JumpPlannerState } from '../useJumpPlanner';
import type { CharacterLocation } from '../useCharacterLocations';
import type { MapStats } from '../useOverlayData';
import { JUMP_SHIPS } from '../jump/constants';
import { securityColorCSS } from '../utils/colors';
import { SystemSlotWithSearch } from './SystemSlotWithSearch';
import { FONT, BG, BORDER, TEXT, MUTED, ACCENT, JUMP_COLOR } from './plannerStyles';

interface Props {
  planner: JumpPlannerState;
  systems: SystemData[];
  systemName: (id: number) => string;
  characters: CharacterLocation[];
  stats: MapStats | null;
  onFocusSystem: (system: SystemData) => void;
  onHighlightSystems: (ids: Set<number> | null) => void;
  isMobile?: boolean;
}

export function JumpPlannerPanel({ planner, systems, systemName, characters, stats, onFocusSystem, onHighlightSystems, isMobile }: Props) {
  const shipEntries = Object.entries(JUMP_SHIPS) as [JumpShipClass, typeof JUMP_SHIPS[JumpShipClass]][];
  const charsWithLocation = characters.filter(c => c.system_id !== null);
  const [showRange, setShowRange] = useState(false);

  // When range view is toggled, highlight reachable systems
  useEffect(() => {
    if (showRange && planner.reachableSystems.length > 0) {
      onHighlightSystems(new Set(planner.reachableSystems.map(s => s.id)));
    } else if (!showRange) {
      // Restore route highlight if route exists
      if (planner.jumpRoute && planner.jumpRoute.length > 1) {
        onHighlightSystems(new Set(planner.jumpRoute.map(wp => wp.system.id)));
      } else {
        onHighlightSystems(null);
      }
    }
  }, [showRange, planner.reachableSystems, planner.jumpRoute, onHighlightSystems]);

  return (
    <div style={{
      position: isMobile ? 'relative' : 'absolute',
      ...(isMobile
        ? { width: '100%', height: '100%' }
        : { top: 48, left: 10, width: 280, maxHeight: 'calc(100% - 100px)' }),
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
        <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.1em', color: JUMP_COLOR }}>
          JUMP PLANNER
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

      {/* Ship class */}
      <Label text="SHIP CLASS" />
      <select
        value={planner.shipClass}
        onChange={e => planner.setShipClass(e.target.value as JumpShipClass)}
        style={{
          width: '100%', padding: '4px 6px', fontSize: 10, fontFamily: FONT,
          background: '#080808', color: TEXT, border: `1px solid ${BORDER}`,
          cursor: 'pointer', marginBottom: 8,
        }}
      >
        {shipEntries.map(([key, cfg]) => (
          <option key={key} value={key}>{cfg.label}</option>
        ))}
      </select>

      {/* Skill levels */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 8 }}>
        <div style={{ flex: 1 }}>
          <Label text="JDC" />
          <SkillButtons value={planner.jdcLevel} onChange={planner.setJdcLevel} />
        </div>
        <div style={{ flex: 1 }}>
          <Label text="JFC" />
          <SkillButtons value={planner.jfcLevel} onChange={planner.setJfcLevel} />
        </div>
      </div>

      {/* Range readout */}
      <div style={{ fontSize: 9, color: MUTED, marginBottom: 10 }}>
        RANGE: <span style={{ color: JUMP_COLOR }}>{planner.range.toFixed(1)} LY</span>
        {planner.jumpOrigin !== null && (
          <span style={{ marginLeft: 8 }}>
            REACHABLE: <span style={{ color: TEXT }}>{planner.reachableIds.size}</span>
          </span>
        )}
      </div>

      {/* Route preferences */}
      <Label text="PREFERENCES" />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 10 }}>
        <ToggleRow
          label="PREFER NPC STATION"
          active={planner.preferStation}
          onToggle={() => planner.setPreferStation(!planner.preferStation)}
        />
        <ToggleRow
          label="PREFER HIGHSEC GATE"
          active={planner.preferHsGate}
          onToggle={() => planner.setPreferHsGate(!planner.preferHsGate)}
        />
      </div>

      {/* Origin */}
      <Label text="ORIGIN" />
      <SystemSlotWithSearch
        systemId={planner.jumpOrigin}
        systems={systems}
        systemName={systemName}
        characters={charsWithLocation}
        onSelect={(id) => planner.setJumpOrigin(id)}
        onClear={() => planner.setJumpOrigin(null)}
        onFocusSystem={onFocusSystem}
        placeholder="Search or click map..."
        isOrigin
        cynoOnly
      />

      {/* Destination */}
      <div style={{ marginTop: 6 }}>
        <Label text="DESTINATION" />
        <SystemSlotWithSearch
          systemId={planner.jumpDest}
          systems={systems}
          systemName={systemName}
          characters={charsWithLocation}
          onSelect={(id) => planner.setJumpDest(id)}
          onClear={() => planner.setJumpDest(null)}
          onFocusSystem={onFocusSystem}
          placeholder="Search or click map..."
          cynoOnly
        />
      </div>

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: 4, marginTop: 10, marginBottom: 8 }}>
        <button
          onClick={planner.calculate}
          disabled={planner.jumpOrigin === null || planner.jumpDest === null}
          style={{
            flex: 1, padding: '6px', fontSize: 10, letterSpacing: '0.12em',
            fontFamily: FONT, textTransform: 'uppercase',
            background: planner.jumpOrigin && planner.jumpDest ? 'rgba(255,136,0,0.15)' : 'transparent',
            color: planner.jumpOrigin && planner.jumpDest ? JUMP_COLOR : MUTED,
            border: `1px solid ${planner.jumpOrigin && planner.jumpDest ? JUMP_COLOR : BORDER}`,
            cursor: planner.jumpOrigin && planner.jumpDest ? 'pointer' : 'default',
          }}
        >
          Route
        </button>
        <button
          onClick={() => setShowRange(prev => !prev)}
          disabled={planner.jumpOrigin === null}
          style={{
            flex: 1, padding: '6px', fontSize: 10, letterSpacing: '0.12em',
            fontFamily: FONT, textTransform: 'uppercase',
            background: showRange ? 'rgba(255,136,0,0.15)' : 'transparent',
            color: planner.jumpOrigin ? JUMP_COLOR : MUTED,
            border: `1px solid ${planner.jumpOrigin ? (showRange ? JUMP_COLOR : BORDER) : BORDER}`,
            cursor: planner.jumpOrigin ? 'pointer' : 'default',
          }}
        >
          Range
        </button>
      </div>

      {planner.routeError && (
        <div style={{ fontSize: 9, color: '#cc3333', marginBottom: 8 }}>
          {planner.routeError}
        </div>
      )}

      {/* Range list */}
      {showRange && planner.reachableSystems.length > 0 && (
        <div>
          <div style={{ fontSize: 8, color: '#3a3a3a', letterSpacing: '0.1em', marginBottom: 4 }}>
            IN RANGE [{planner.reachableSystems.length}]
          </div>
          <div style={{ maxHeight: 250, overflowY: 'auto', border: `1px solid ${BORDER}`, background: '#0a0a0a' }}>
            {planner.reachableSystems
              .slice()
              .sort((a, b) => {
                // Stations first, then by name
                if (a.hasStation !== b.hasStation) return a.hasStation ? -1 : 1;
                return a.name.localeCompare(b.name);
              })
              .map(sys => (
                <SystemRowWithStats
                  key={sys.id}
                  system={sys}
                  stats={stats}
                  onClick={() => {
                    planner.setJumpDest(sys.id);
                    setShowRange(false);
                  }}
                />
              ))}
          </div>
        </div>
      )}

      {/* Route results */}
      {!showRange && planner.jumpRoute && planner.jumpRoute.length > 1 && (
        <RouteResults
          planner={planner}
          stats={stats}
          onFocusSystem={onFocusSystem}
          onHighlightSystems={onHighlightSystems}
        />
      )}
    </div>
  );
}

/* ── Route Results with editable midpoints ──────────────────────── */

function RouteResults({ planner, stats, onFocusSystem, onHighlightSystems }: {
  planner: JumpPlannerState;
  stats: MapStats | null;
  onFocusSystem: (s: SystemData) => void;
  onHighlightSystems: (ids: Set<number> | null) => void;
}) {
  const route = planner.jumpRoute!;
  const last = route[route.length - 1];
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [insertAfterIndex, setInsertAfterIndex] = useState<number | null>(null);

  return (
    <div>
      <div style={{ fontSize: 9, color: MUTED, letterSpacing: '0.1em', marginBottom: 6 }}>
        ROUTE: {route.length - 1} JUMP{route.length > 2 ? 'S' : ''}
      </div>

      {route.map((wp, i) => {
        const isOrigin = i === 0;
        const isDest = i === route.length - 1;
        const isMidpoint = !isOrigin && !isDest;
        const sid = String(wp.system.id);
        const kills = stats?.kills[sid];
        const jumps = stats?.jumps[sid];

        return (
          <div key={`${wp.system.id}-${i}`}>
            <div style={{
              padding: '5px 0',
              borderTop: i > 0 ? `1px solid ${BORDER}` : 'none',
              fontSize: 9,
            }}>
              {/* System name row */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span
                  style={{ color: isOrigin ? ACCENT : TEXT, cursor: 'pointer', flex: 1 }}
                  onClick={() => onFocusSystem(wp.system)}
                >
                  {i + 1}. {wp.system.name}
                  <span style={{ color: securityColorCSS(wp.system.sec), marginLeft: 3, fontSize: 8 }}>
                    {wp.system.sec.toFixed(1)}
                  </span>
                </span>

                {/* NPC Station badge */}
                {wp.system.hasStation && (
                  <span style={{ fontSize: 7, color: '#33aa55', letterSpacing: '0.08em' }} title="NPC Station">
                    STN
                  </span>
                )}

                {/* Distance */}
                {wp.distanceLY > 0 && (
                  <span style={{ color: MUTED, fontSize: 8 }}>
                    {wp.distanceLY.toFixed(2)}LY
                  </span>
                )}

                {/* Midpoint edit controls */}
                {isMidpoint && (
                  <span style={{ display: 'flex', gap: 2 }}>
                    <MiniBtn title="Swap midpoint" onClick={() => setEditingIndex(editingIndex === i ? null : i)}>↔</MiniBtn>
                    <MiniBtn title="Remove midpoint" onClick={() => { planner.removeMidpoint(i); setEditingIndex(null); }}>×</MiniBtn>
                  </span>
                )}
              </div>

              {/* Activity stats */}
              {(kills || jumps) && (
                <div style={{ fontSize: 7, color: '#3a3a3a', marginTop: 2, display: 'flex', gap: 6 }}>
                  {jumps !== undefined && jumps > 0 && <span>JMP {jumps.toLocaleString()}</span>}
                  {kills?.ship !== undefined && kills.ship > 0 && <span style={{ color: '#993333' }}>SK {kills.ship}</span>}
                  {kills?.npc !== undefined && kills.npc > 0 && <span>NK {kills.npc.toLocaleString()}</span>}
                  {kills?.pod !== undefined && kills.pod > 0 && <span style={{ color: '#993333' }}>PK {kills.pod}</span>}
                </div>
              )}

              {/* Fuel / fatigue */}
              {wp.fuelThisHop > 0 && (
                <div style={{ color: MUTED, fontSize: 8, marginTop: 2 }}>
                  FUEL {wp.fuelThisHop.toLocaleString()}
                  {wp.waitMinutes > 0 && (
                    <span style={{ marginLeft: 6, color: '#cc8833' }}>WAIT {wp.waitMinutes.toFixed(1)}m</span>
                  )}
                  {wp.blueFatigue > 0 && (
                    <span style={{ marginLeft: 6, color: '#4488cc' }}>FAT {wp.blueFatigue.toFixed(0)}m</span>
                  )}
                </div>
              )}

              {/* Midpoint swap search */}
              {editingIndex === i && (
                <MidpointSearch
                  alternatives={planner.getAlternatives(i)}
                  stats={stats}
                  onSelect={(id) => { planner.replaceMidpoint(i, id); setEditingIndex(null); }}
                  onCancel={() => { setEditingIndex(null); onHighlightSystems(null); }}
                  onHighlightSystems={onHighlightSystems}
                />
              )}
            </div>

            {/* Insert midpoint button between hops */}
            {!isDest && (
              <div style={{ display: 'flex', justifyContent: 'center', padding: '1px 0' }}>
                <button
                  onClick={() => setInsertAfterIndex(insertAfterIndex === i ? null : i)}
                  title="Add midpoint here"
                  style={{
                    background: 'none', border: 'none', color: insertAfterIndex === i ? JUMP_COLOR : '#1a1a1a',
                    cursor: 'pointer', fontSize: 10, fontFamily: FONT, padding: '0 4px',
                  }}
                >
                  +
                </button>
              </div>
            )}

            {/* Insert midpoint search */}
            {insertAfterIndex === i && (
              <div style={{ padding: '4px 0' }}>
                <MidpointSearch
                  alternatives={planner.getInsertAlternatives(i)}
                  stats={stats}
                  onSelect={(id) => { planner.insertMidpoint(i, id); setInsertAfterIndex(null); }}
                  onCancel={() => { setInsertAfterIndex(null); onHighlightSystems(null); }}
                  onHighlightSystems={onHighlightSystems}
                />
              </div>
            )}
          </div>
        );
      })}

      {/* Totals */}
      <div style={{
        marginTop: 8, padding: '6px 0', borderTop: `1px solid ${BORDER}`,
        fontSize: 9, color: JUMP_COLOR,
      }}>
        <div>TOTAL FUEL: {last.cumulativeFuel.toLocaleString()}</div>
        <div>TOTAL TIME: {last.cumulativeMinutes.toFixed(1)} MIN</div>
      </div>
    </div>
  );
}

/* ── Midpoint search dropdown ──────────────────────────────────── */

function MidpointSearch({ alternatives, stats, onSelect, onCancel, onHighlightSystems }: {
  alternatives: SystemData[];
  stats: MapStats | null;
  onSelect: (id: number) => void;
  onCancel: () => void;
  onHighlightSystems: (ids: Set<number> | null) => void;
}) {
  // Highlight all alternatives on the map
  useEffect(() => {
    if (alternatives.length > 0) {
      onHighlightSystems(new Set(alternatives.map(s => s.id)));
    }
    return () => { onHighlightSystems(null); };
  }, [alternatives, onHighlightSystems]);

  return (
    <div style={{
      marginTop: 4, background: '#0a0a0a', border: `1px solid ${BORDER}`,
      padding: 4, fontSize: 9,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
        <span style={{ fontSize: 7, color: '#3a3a3a', letterSpacing: '0.1em' }}>
          ALTERNATIVES [{alternatives.length}]
        </span>
        <button onClick={onCancel} style={{
          background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
          fontSize: 10, fontFamily: FONT,
        }}>×</button>
      </div>

      <div style={{ maxHeight: 200, overflowY: 'auto' }}>
        {alternatives.length === 0 && (
          <div style={{ color: '#2a2a2a', padding: '2px 4px', fontSize: 8 }}>
            No reachable alternatives
          </div>
        )}
        {alternatives.map(sys => (
          <SystemRowWithStats key={sys.id} system={sys} stats={stats} onClick={() => onSelect(sys.id)} />
        ))}
      </div>
    </div>
  );
}

function SystemRowWithStats({ system, stats, onClick }: {
  system: SystemData;
  stats: MapStats | null;
  onClick: () => void;
}) {
  const sid = String(system.id);
  const kills = stats?.kills[sid];
  const jumps = stats?.jumps[sid];
  const hasActivity = (kills?.ship ?? 0) > 0 || (kills?.npc ?? 0) > 0 || (jumps ?? 0) > 0;

  return (
    <div
      onClick={onClick}
      style={{
        padding: '3px 4px', cursor: 'pointer', fontSize: 9,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ color: TEXT }}>
          {system.name}
          {system.hasStation && (
            <span style={{ color: '#33aa55', marginLeft: 3, fontSize: 7 }}>STN</span>
          )}
        </span>
        <span style={{ color: securityColorCSS(system.sec), fontSize: 8 }}>
          {system.sec.toFixed(1)}
        </span>
      </div>
      {hasActivity && (
        <div style={{ fontSize: 7, color: '#3a3a3a', display: 'flex', gap: 5, marginTop: 1 }}>
          {(jumps ?? 0) > 0 && <span>JMP {jumps}</span>}
          {(kills?.ship ?? 0) > 0 && <span style={{ color: '#993333' }}>SK {kills!.ship}</span>}
          {(kills?.npc ?? 0) > 0 && <span>NK {kills!.npc.toLocaleString()}</span>}
          {(kills?.pod ?? 0) > 0 && <span style={{ color: '#993333' }}>PK {kills!.pod}</span>}
        </div>
      )}
    </div>
  );
}

/* ── Shared Components ─────────────────────────────────────────── */

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

function SkillButtons({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div style={{ display: 'flex', gap: 2 }}>
      {[1, 2, 3, 4, 5].map(level => (
        <button
          key={level}
          onClick={() => onChange(level)}
          style={{
            width: 22, height: 20, fontSize: 9, fontFamily: FONT,
            background: level === value ? 'rgba(200,169,81,0.15)' : 'transparent',
            color: level === value ? ACCENT : MUTED,
            border: `1px solid ${level === value ? ACCENT : BORDER}`,
            cursor: 'pointer',
          }}
        >
          {level}
        </button>
      ))}
    </div>
  );
}

function ToggleRow({ label, active, onToggle }: { label: string; active: boolean; onToggle: () => void }) {
  return (
    <div
      onClick={onToggle}
      style={{
        display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer',
        fontSize: 8, letterSpacing: '0.1em', color: active ? TEXT : MUTED,
      }}
    >
      <span style={{
        width: 12, height: 12, display: 'flex', alignItems: 'center', justifyContent: 'center',
        border: `1px solid ${active ? JUMP_COLOR : BORDER}`,
        background: active ? 'rgba(255,136,0,0.15)' : 'transparent',
        color: active ? JUMP_COLOR : 'transparent',
        fontSize: 9, fontFamily: FONT,
      }}>
        {active ? '✓' : ''}
      </span>
      {label}
    </div>
  );
}

function MiniBtn({ children, onClick, title }: { children: string; onClick: () => void; title: string }) {
  return (
    <button
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      title={title}
      style={{
        background: 'none', border: `1px solid ${BORDER}`, color: MUTED,
        cursor: 'pointer', fontSize: 9, fontFamily: FONT, padding: '0 3px',
        lineHeight: '14px',
      }}
    >
      {children}
    </button>
  );
}
