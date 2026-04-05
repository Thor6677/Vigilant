import { useState, useRef, useMemo } from 'react';
import type { SystemData, JumpShipClass } from '../types';
import type { JumpPlannerState } from '../useJumpPlanner';
import type { CharacterLocation } from '../useCharacterLocations';
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
  onFocusSystem: (system: SystemData) => void;
}

export function JumpPlannerPanel({ planner, systems, systemName, characters, onFocusSystem }: Props) {
  const shipEntries = Object.entries(JUMP_SHIPS) as [JumpShipClass, typeof JUMP_SHIPS[JumpShipClass]][];
  const charsWithLocation = characters.filter(c => c.system_id !== null);

  return (
    <div style={{
      position: 'absolute',
      top: 48,
      left: 10,
      width: 270,
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
        <button onClick={() => planner.setActive(false)} style={{
          background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
          fontSize: 14, fontFamily: FONT, lineHeight: 1,
        }}>×</button>
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

      {/* Origin */}
      <Label text="ORIGIN" />
      <SystemSlotWithSearch
        systemId={planner.jumpOrigin}
        systems={systems}
        systemName={systemName}
        characters={charsWithLocation}
        onSelect={(id) => { planner.setJumpOrigin(id); }}
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
          onSelect={(id) => { planner.setJumpDest(id); }}
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

      {/* Error */}
      {planner.routeError && (
        <div style={{ fontSize: 9, color: '#cc3333', marginBottom: 8 }}>
          {planner.routeError}
        </div>
      )}

      {/* Route results */}
      {planner.jumpRoute && planner.jumpRoute.length > 1 && (
        <RouteResults planner={planner} onFocusSystem={onFocusSystem} />
      )}
    </div>
  );
}

/* ── Route Results ─────────────────────────────────────────────── */

function RouteResults({ planner, onFocusSystem }: { planner: JumpPlannerState; onFocusSystem: (s: SystemData) => void }) {
  const route = planner.jumpRoute!;
  const last = route[route.length - 1];

  return (
    <div>
      <div style={{ fontSize: 9, color: MUTED, letterSpacing: '0.1em', marginBottom: 6 }}>
        ROUTE: {route.length - 1} JUMP{route.length > 2 ? 'S' : ''}
      </div>

      {route.map((wp, i) => (
        <div key={wp.system.id} style={{
          padding: '4px 0',
          borderTop: i > 0 ? `1px solid ${BORDER}` : 'none',
          fontSize: 9,
          cursor: 'pointer',
        }} onClick={() => onFocusSystem(wp.system)}>
          <div style={{ color: i === 0 ? ACCENT : TEXT }}>
            {i + 1}. {wp.system.name}
            <span style={{ color: securityColorCSS(wp.system.sec), marginLeft: 4, fontSize: 8 }}>
              {wp.system.sec.toFixed(1)}
            </span>
            {wp.distanceLY > 0 && (
              <span style={{ color: MUTED, marginLeft: 6 }}>
                {wp.distanceLY.toFixed(2)} LY
              </span>
            )}
          </div>
          {wp.fuelThisHop > 0 && (
            <div style={{ color: MUTED, fontSize: 8, marginTop: 1 }}>
              FUEL {wp.fuelThisHop.toLocaleString()}
              {wp.waitMinutes > 0 && (
                <span style={{ marginLeft: 8, color: '#cc8833' }}>
                  WAIT {wp.waitMinutes.toFixed(1)}m
                </span>
              )}
              {wp.blueFatigue > 0 && (
                <span style={{ marginLeft: 8, color: '#4488cc' }}>
                  FAT {wp.blueFatigue.toFixed(0)}m
                </span>
              )}
            </div>
          )}
        </div>
      ))}

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

/* ── System Slot with Inline Search + Character Locations ──────── */

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
  const inputRef = useRef<HTMLInputElement>(null);

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
          title="Click to focus on map"
        >
          {systemName(systemId)}
          {sys && (
            <span style={{ color: securityColorCSS(sys.sec), marginLeft: 4, fontSize: 9 }}>
              {sys.sec.toFixed(1)}
            </span>
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
      {/* Character location shortcuts */}
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

      {/* Search input */}
      <div style={{
        display: 'flex', alignItems: 'center',
        padding: '3px 6px', background: '#080808', border: `1px solid ${BORDER}`,
        minHeight: 26,
      }}>
        <input
          ref={inputRef}
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

      {/* Search dropdown */}
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
                  if (!disabled) {
                    onSelect(sys.id);
                    setQuery('');
                    setSearching(false);
                  }
                }}
                style={{
                  padding: '4px 6px', fontSize: 9, fontFamily: FONT, letterSpacing: '0.06em',
                  cursor: disabled ? 'default' : 'pointer',
                  display: 'flex', justifyContent: 'space-between',
                  color: disabled ? '#2a2a2a' : TEXT,
                }}
                title={disabled ? 'Highsec — cannot light cyno' : ''}
              >
                <span>{sys.name}</span>
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
