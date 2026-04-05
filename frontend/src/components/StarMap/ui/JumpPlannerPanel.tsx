import { useState, useRef, useMemo, useEffect } from 'react';
import type { SystemData, JumpShipClass } from '../types';
import type { JumpPlannerState } from '../useJumpPlanner';
import type { CharacterLocation } from '../useCharacterLocations';
import type { MapStats } from '../useOverlayData';
import { JUMP_SHIPS } from '../jump/constants';
import { securityColorCSS } from '../utils/colors';
import { canLightCyno } from '../jump/distance';

const FONT = "'JetBrains Mono', monospace";
const BG = 'rgba(14, 14, 14, 0.97)';
const BORDER = '#191919';
const TEXT = '#dedede';
const MUTED = '#474747';
const ACCENT = '#c8a951';
const JUMP_COLOR = '#ff8800';

interface Props {
  planner: JumpPlannerState;
  systems: SystemData[];
  systemName: (id: number) => string;
  characters: CharacterLocation[];
  stats: MapStats | null;
  onFocusSystem: (system: SystemData) => void;
  onHighlightSystems: (ids: Set<number> | null) => void;
}

export function JumpPlannerPanel({ planner, systems, systemName, characters, stats, onFocusSystem, onHighlightSystems }: Props) {
  const shipEntries = Object.entries(JUMP_SHIPS) as [JumpShipClass, typeof JUMP_SHIPS[JumpShipClass]][];
  const charsWithLocation = characters.filter(c => c.system_id !== null);

  return (
    <div style={{
      position: 'absolute',
      top: 48,
      left: 10,
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
        />
      </div>

      {/* Calculate button */}
      <button
        onClick={planner.calculate}
        disabled={planner.jumpOrigin === null || planner.jumpDest === null}
        style={{
          width: '100%', padding: '6px', fontSize: 10, letterSpacing: '0.12em',
          fontFamily: FONT, textTransform: 'uppercase', marginTop: 10,
          background: planner.jumpOrigin && planner.jumpDest ? 'rgba(255,136,0,0.15)' : 'transparent',
          color: planner.jumpOrigin && planner.jumpDest ? JUMP_COLOR : MUTED,
          border: `1px solid ${planner.jumpOrigin && planner.jumpDest ? JUMP_COLOR : BORDER}`,
          cursor: planner.jumpOrigin && planner.jumpDest ? 'pointer' : 'default',
          marginBottom: 8,
        }}
      >
        Calculate Route
      </button>

      {planner.routeError && (
        <div style={{ fontSize: 9, color: '#cc3333', marginBottom: 8 }}>
          {planner.routeError}
        </div>
      )}

      {/* Route results */}
      {planner.jumpRoute && planner.jumpRoute.length > 1 && (
        <RouteResults
          planner={planner}
          systems={systems}
          stats={stats}
          onFocusSystem={onFocusSystem}
          onHighlightSystems={onHighlightSystems}
        />
      )}
    </div>
  );
}

/* ── Route Results with editable midpoints ──────────────────────── */

function RouteResults({ planner, systems, stats, onFocusSystem, onHighlightSystems }: {
  planner: JumpPlannerState;
  systems: SystemData[];
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
                  allSystems={systems}
                  stats={stats}
                  onSelect={(id) => { planner.replaceMidpoint(i, id); setEditingIndex(null); onHighlightSystems(null); }}
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
                  allSystems={systems}
                  stats={stats}
                  isInsert
                  onSelect={(id) => { planner.insertMidpoint(i, id); setInsertAfterIndex(null); onHighlightSystems(null); }}
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

function MidpointSearch({ alternatives, allSystems, stats, onSelect, onCancel, onHighlightSystems, isInsert }: {
  alternatives: SystemData[];
  allSystems: SystemData[];
  stats: MapStats | null;
  onSelect: (id: number) => void;
  onCancel: () => void;
  onHighlightSystems: (ids: Set<number> | null) => void;
  isInsert?: boolean;
}) {
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const searchResults = useMemo(() => {
    if (query.length < 2) return [];
    const q = query.toLowerCase();
    return allSystems.filter(s => s.name.toLowerCase().includes(q) && canLightCyno(s)).slice(0, 6);
  }, [query, allSystems]);

  const showAlts = query.length < 2 && alternatives.length > 0;

  // Highlight alternatives on map when dropdown opens
  useEffect(() => {
    const systemsToShow = showAlts ? alternatives : searchResults;
    if (systemsToShow.length > 0) {
      onHighlightSystems(new Set(systemsToShow.map(s => s.id)));
    } else {
      onHighlightSystems(null);
    }
  }, [showAlts, alternatives, searchResults, onHighlightSystems]);

  return (
    <div style={{
      marginTop: 4, background: '#0a0a0a', border: `1px solid ${BORDER}`,
      padding: 4, fontSize: 9,
    }}>
      <div style={{ display: 'flex', gap: 4, marginBottom: 4 }}>
        <input
          ref={inputRef}
          autoFocus
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder={isInsert ? 'Search system to add...' : 'Search replacement...'}
          style={{
            flex: 1, background: 'none', border: `1px solid ${BORDER}`, outline: 'none',
            color: TEXT, fontSize: 9, fontFamily: FONT, padding: '2px 4px',
          }}
        />
        <button onClick={onCancel} style={{
          background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
          fontSize: 10, fontFamily: FONT,
        }}>×</button>
      </div>

      <div style={{ maxHeight: 150, overflowY: 'auto' }}>
        {showAlts && (
          <>
            <div style={{ fontSize: 7, color: '#2a2a2a', letterSpacing: '0.1em', padding: '2px 0' }}>
              REACHABLE ALTERNATIVES ({alternatives.length})
            </div>
            {alternatives.slice(0, 10).map(sys => (
              <SystemRowWithStats key={sys.id} system={sys} stats={stats} onClick={() => onSelect(sys.id)} />
            ))}
            {alternatives.length > 10 && (
              <div style={{ fontSize: 7, color: '#2a2a2a', padding: '2px 4px' }}>
                ... {alternatives.length - 10} more
              </div>
            )}
          </>
        )}
        {searchResults.map(sys => (
          <SystemRowWithStats key={sys.id} system={sys} stats={stats} onClick={() => onSelect(sys.id)} />
        ))}
        {query.length >= 2 && searchResults.length === 0 && (
          <div style={{ color: '#2a2a2a', padding: '2px 4px', fontSize: 8 }}>No results</div>
        )}
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

function SystemSlotWithSearch({
  systemId, systems, systemName, characters, onSelect, onClear, onFocusSystem, placeholder, isOrigin,
}: {
  systemId: number | null;
  systems: SystemData[];
  systemName: (id: number) => string;
  characters: CharacterLocation[];
  onSelect: (id: number) => void;
  onClear: () => void;
  onFocusSystem: (system: SystemData) => void;
  placeholder: string;
  isOrigin?: boolean;
}) {
  const [searching, setSearching] = useState(false);
  const [query, setQuery] = useState('');

  const results = useMemo(() => {
    if (!query || query.length < 2) return [];
    const q = query.toLowerCase();
    const matches: SystemData[] = [];
    for (const sys of systems) {
      if (sys.name.toLowerCase().includes(q)) {
        matches.push(sys);
        if (matches.length >= 8) break;
      }
    }
    return matches;
  }, [query, systems]);

  if (systemId !== null) {
    const sys = systems.find(s => s.id === systemId);
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '4px 6px', background: '#080808', border: `1px solid ${BORDER}`,
        fontSize: 10, minHeight: 26,
      }}>
        <span
          style={{ color: TEXT, cursor: 'pointer' }}
          onClick={() => { if (sys) onFocusSystem(sys); }}
        >
          {systemName(systemId)}
          {sys && (
            <>
              <span style={{ color: securityColorCSS(sys.sec), marginLeft: 4, fontSize: 9 }}>
                {sys.sec.toFixed(1)}
              </span>
              {sys.hasStation && (
                <span style={{ color: '#33aa55', marginLeft: 3, fontSize: 7 }}>STN</span>
              )}
            </>
          )}
        </span>
        <button onClick={onClear} style={{
          background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
          fontSize: 12, fontFamily: FONT, lineHeight: 1,
        }}>×</button>
      </div>
    );
  }

  return (
    <div style={{ position: 'relative' }}>
      {characters.length > 0 && !searching && (
        <div style={{ display: 'flex', gap: 3, marginBottom: 3, flexWrap: 'wrap' }}>
          {characters.map(ch => (
            <button
              key={ch.character_id}
              onClick={() => ch.system_id && onSelect(ch.system_id)}
              title={`${ch.character_name} @ ${ch.system_name}`}
              style={{
                padding: '2px 6px', fontSize: 8, fontFamily: FONT, letterSpacing: '0.05em',
                background: 'rgba(200,169,81,0.08)', color: ACCENT,
                border: `1px solid rgba(200,169,81,0.2)`, cursor: 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              {ch.character_name.split(' ')[0]}
            </button>
          ))}
        </div>
      )}

      <div style={{
        display: 'flex', alignItems: 'center',
        padding: '3px 6px', background: '#080808', border: `1px solid ${BORDER}`, minHeight: 26,
      }}>
        <input
          type="text"
          value={query}
          onChange={e => { setQuery(e.target.value); setSearching(true); }}
          onFocus={() => setSearching(true)}
          onBlur={() => setTimeout(() => { setSearching(false); setQuery(''); }, 150)}
          placeholder={placeholder}
          style={{
            flex: 1, background: 'none', border: 'none', outline: 'none',
            color: TEXT, fontSize: 9, fontFamily: FONT, letterSpacing: '0.08em',
          }}
        />
      </div>

      {searching && results.length > 0 && (
        <div style={{
          position: 'absolute', left: 0, right: 0, top: '100%',
          background: BG, border: `1px solid ${BORDER}`, borderTop: 'none',
          maxHeight: 160, overflowY: 'auto', zIndex: 40,
        }}>
          {results.map(sys => {
            const isCyno = canLightCyno(sys);
            const disabled = !isOrigin && !isCyno;
            return (
              <div
                key={sys.id}
                onMouseDown={() => {
                  if (!disabled) { onSelect(sys.id); setQuery(''); setSearching(false); }
                }}
                style={{
                  padding: '4px 6px', fontSize: 9, fontFamily: FONT,
                  cursor: disabled ? 'default' : 'pointer',
                  display: 'flex', justifyContent: 'space-between',
                  color: disabled ? '#2a2a2a' : TEXT,
                }}
              >
                <span>
                  {sys.name}
                  {sys.hasStation && <span style={{ color: '#33aa55', marginLeft: 3, fontSize: 7 }}>STN</span>}
                </span>
                <span style={{ color: securityColorCSS(sys.sec), fontSize: 8 }}>
                  {sys.sec.toFixed(1)}
                  {disabled && <span style={{ color: '#cc3333', marginLeft: 4 }}>HS</span>}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

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
