import { useState, useRef, useMemo, useCallback, useEffect } from 'react';
import type { SystemData } from '../types';
import type { CharacterLocation } from '../useCharacterLocations';
import { securityColorCSS } from '../utils/colors';
import { jumpDistanceLY } from '../jump/distance';

const FONT = "'JetBrains Mono', monospace";
const MAX_RESULTS = 15;

export type SearchResultType = 'system' | 'constellation' | 'region' | 'service';

export interface SearchResult {
  type: SearchResultType;
  id: number;
  name: string;
  system?: SystemData;
  systemCount?: number;
  secRange?: string;
  // Service search
  serviceName?: string;
  distanceLY?: number;
  characterName?: string;
}

// Service keywords users might search for
const SERVICE_KEYWORDS: Record<string, { key: string; label: string }> = {
  'clone': { key: 'cloning', label: 'Cloning' },
  'cloning': { key: 'cloning', label: 'Cloning' },
  'jump clone': { key: 'jumpClone', label: 'Jump Clone' },
  'jumpclone': { key: 'jumpClone', label: 'Jump Clone' },
  'jc': { key: 'jumpClone', label: 'Jump Clone' },
  'factory': { key: 'factory', label: 'Manufacturing' },
  'manufacturing': { key: 'factory', label: 'Manufacturing' },
  'mfg': { key: 'factory', label: 'Manufacturing' },
  'lab': { key: 'lab', label: 'Research Lab' },
  'laboratory': { key: 'lab', label: 'Research Lab' },
  'research': { key: 'lab', label: 'Research Lab' },
  'market': { key: 'market', label: 'Market' },
  'refinery': { key: 'refinery', label: 'Refinery' },
  'repair': { key: 'repair', label: 'Repair' },
  'reprocessing': { key: 'reprocessing', label: 'Reprocessing' },
  'reprocess': { key: 'reprocessing', label: 'Reprocessing' },
};

interface Props {
  systems: SystemData[];
  characters: CharacterLocation[];
  onSelectSystem: (system: SystemData) => void;
  onSelectArea: (type: 'constellation' | 'region', id: number, name: string) => void;
  /** Optional gate-route action callbacks. When provided, small action buttons
   *  appear on each system/service result row. */
  onSetRouteOrigin?: (id: number) => void;
  onSetRouteDest?: (id: number) => void;
  onAddRouteWaypoint?: (id: number) => void;
  onAvoidSystem?: (id: number) => void;
}

export function SystemSearch({
  systems,
  characters,
  onSelectSystem,
  onSelectArea,
  onSetRouteOrigin,
  onSetRouteDest,
  onAddRouteWaypoint,
  onAvoidSystem,
}: Props) {
  const [query, setQuery] = useState('');
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const { regionList, constellationList } = useMemo(() => {
    const regMap = new Map<number, { name: string; count: number; minSec: number; maxSec: number }>();
    const conMap = new Map<number, { name: string; regName: string; count: number; minSec: number; maxSec: number }>();

    for (const sys of systems) {
      let r = regMap.get(sys.regId);
      if (!r) { r = { name: sys.regName, count: 0, minSec: sys.sec, maxSec: sys.sec }; regMap.set(sys.regId, r); }
      r.count++;
      r.minSec = Math.min(r.minSec, sys.sec);
      r.maxSec = Math.max(r.maxSec, sys.sec);

      let c = conMap.get(sys.conId);
      if (!c) { c = { name: sys.conName, regName: sys.regName, count: 0, minSec: sys.sec, maxSec: sys.sec }; conMap.set(sys.conId, c); }
      c.count++;
      c.minSec = Math.min(c.minSec, sys.sec);
      c.maxSec = Math.max(c.maxSec, sys.sec);
    }

    return {
      regionList: Array.from(regMap.entries()).map(([id, r]) => ({ id, ...r })),
      constellationList: Array.from(conMap.entries()).map(([id, c]) => ({ id, ...c })),
    };
  }, [systems]);

  // System map for character lookups
  const systemMap = useMemo(() => {
    const m = new Map<number, SystemData>();
    for (const sys of systems) m.set(sys.id, sys);
    return m;
  }, [systems]);

  const { results, totalCount } = useMemo(() => {
    if (!query || query.length < 2) return { results: [] as SearchResult[], totalCount: 0 };
    const q = query.toLowerCase();
    const out: SearchResult[] = [];
    let total = 0;

    // Check if query matches a service keyword
    const matchedService = SERVICE_KEYWORDS[q] ?? Object.entries(SERVICE_KEYWORDS).find(([kw]) => kw.startsWith(q))?.[1];

    if (matchedService) {
      // Find nearest systems with this service, sorted by distance from each character
      const charsWithLoc = characters.filter(c => c.system_id !== null);
      const withService = systems.filter(s => s.svcs.includes(matchedService.key));

      if (charsWithLoc.length > 0) {
        // For each character, find the closest systems with this service
        for (const char of charsWithLoc) {
          const charSys = systemMap.get(char.system_id!);
          if (!charSys) continue;

          const sorted = withService
            .map(s => ({ sys: s, dist: jumpDistanceLY(charSys, s) }))
            .sort((a, b) => a.dist - b.dist)
            .slice(0, 5);

          for (const { sys, dist } of sorted) {
            total++;
            if (out.length < MAX_RESULTS) {
              out.push({
                type: 'service',
                id: sys.id,
                name: sys.name,
                system: sys,
                serviceName: matchedService.label,
                distanceLY: dist,
                characterName: char.character_name,
              });
            }
          }
        }
      } else {
        // No character location — just list systems with the service
        for (const sys of withService.slice(0, MAX_RESULTS)) {
          total++;
          out.push({
            type: 'service',
            id: sys.id,
            name: sys.name,
            system: sys,
            serviceName: matchedService.label,
          });
        }
      }

      return { results: out, totalCount: total };
    }

    // 1. Matching regions
    for (const reg of regionList) {
      if (reg.name.toLowerCase().includes(q)) {
        total++;
        if (out.length < MAX_RESULTS) {
          out.push({
            type: 'region', id: reg.id, name: reg.name,
            systemCount: reg.count,
            secRange: `${reg.minSec.toFixed(1)} – ${reg.maxSec.toFixed(1)}`,
          });
        }
      }
    }

    // 2. Matching constellations
    for (const con of constellationList) {
      if (con.name.toLowerCase().includes(q)) {
        total++;
        if (out.length < MAX_RESULTS) {
          out.push({
            type: 'constellation', id: con.id, name: con.name,
            systemCount: con.count,
            secRange: `${con.minSec.toFixed(1)} – ${con.maxSec.toFixed(1)}`,
          });
        }
      }
    }

    // 3. Matching systems
    for (const sys of systems) {
      if (sys.name.toLowerCase().includes(q)) {
        total++;
        if (out.length < MAX_RESULTS) {
          out.push({ type: 'system', id: sys.id, name: sys.name, system: sys });
        }
      }
    }

    return { results: out, totalCount: total };
  }, [query, systems, regionList, constellationList, characters, systemMap]);

  const handleSelect = useCallback((result: SearchResult) => {
    setQuery('');
    setFocused(false);
    inputRef.current?.blur();

    if ((result.type === 'system' || result.type === 'service') && result.system) {
      onSelectSystem(result.system);
    } else if (result.type === 'constellation' || result.type === 'region') {
      onSelectArea(result.type, result.id, result.name);
    }
  }, [onSelectSystem, onSelectArea]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex(i => Math.min(i + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex(i => Math.max(i - 1, 0));
    } else if (e.key === 'Enter' && results[activeIndex]) {
      e.preventDefault();
      handleSelect(results[activeIndex]);
    } else if (e.key === 'Escape') {
      setQuery('');
      setFocused(false);
      inputRef.current?.blur();
    }
  }, [results, activeIndex, handleSelect]);

  useEffect(() => setActiveIndex(0), [results]);

  useEffect(() => {
    function handleGlobalKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === 'f' || e.key === '/') {
        e.preventDefault();
        inputRef.current?.focus();
        setFocused(true);
      }
    }
    window.addEventListener('keydown', handleGlobalKey);
    return () => window.removeEventListener('keydown', handleGlobalKey);
  }, []);

  const showDropdown = focused && results.length > 0;
  const hasMore = totalCount > MAX_RESULTS;

  return (
    <div style={{
      position: 'absolute',
      top: 10,
      left: 10,
      zIndex: 30,
      width: 280,
    }}>
      <div style={{ position: 'relative' }}>
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          onFocus={() => setFocused(true)}
          onBlur={() => setTimeout(() => setFocused(false), 150)}
          onKeyDown={handleKeyDown}
          placeholder="SEARCH SYSTEMS, SERVICES... (F)"
          aria-label="Search systems, regions, services"
          style={{
            width: '100%',
            padding: '7px 28px 7px 10px',
            fontSize: 10,
            letterSpacing: '0.1em',
            fontFamily: FONT,
            background: 'rgba(14, 14, 14, 0.95)',
            color: '#dedede',
            border: '1px solid #191919',
            outline: 'none',
            textTransform: 'uppercase',
          }}
        />
        {query.length > 0 && (
          <button
            onMouseDown={(e) => {
              e.preventDefault();
              setQuery('');
              inputRef.current?.focus();
            }}
            style={{
              position: 'absolute',
              right: 6,
              top: '50%',
              transform: 'translateY(-50%)',
              background: 'none',
              border: 'none',
              color: '#474747',
              cursor: 'pointer',
              fontSize: 14,
              fontFamily: FONT,
              padding: '0 2px',
              lineHeight: 1,
            }}
          >
            ×
          </button>
        )}
      </div>

      {showDropdown && (
        <div style={{
          background: 'rgba(14, 14, 14, 0.97)',
          border: '1px solid #191919',
          borderTop: 'none',
          maxHeight: 300,
          overflowY: 'auto',
        }}>
          {results.map((result, i) => (
            <div
              key={`${result.type}-${result.id}-${result.characterName ?? ''}`}
              onMouseDown={() => handleSelect(result)}
              style={{
                padding: '6px 10px',
                fontSize: 10,
                fontFamily: FONT,
                cursor: 'pointer',
                background: i === activeIndex ? 'rgba(200,169,81,0.07)' : 'transparent',
                letterSpacing: '0.08em',
                borderTop: i > 0 && result.type !== results[i - 1].type ? '1px solid #191919' : 'none',
              }}
            >
              {result.type === 'system' && result.system ? (
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ color: '#dedede' }}>{result.name}</span>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    <RouteActionGroup
                      systemId={result.id}
                      onSetRouteOrigin={onSetRouteOrigin}
                      onSetRouteDest={onSetRouteDest}
                      onAddRouteWaypoint={onAddRouteWaypoint}
                      onAvoidSystem={onAvoidSystem}
                    />
                    <span style={{ fontSize: 9, color: '#474747' }}>
                      <span style={{ color: securityColorCSS(result.system.sec), marginRight: 6 }}>
                        {result.system.sec.toFixed(1)}
                      </span>
                      {result.system.regName}
                    </span>
                  </span>
                </div>
              ) : result.type === 'service' && result.system ? (
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
                      <span style={{ fontSize: 8, color: '#33aa55', letterSpacing: '0.1em' }}>
                        {result.serviceName}
                      </span>
                      <span style={{ color: '#dedede' }}>{result.name}</span>
                    </span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <RouteActionGroup
                        systemId={result.id}
                        onSetRouteOrigin={onSetRouteOrigin}
                        onSetRouteDest={onSetRouteDest}
                        onAddRouteWaypoint={onAddRouteWaypoint}
                        onAvoidSystem={onAvoidSystem}
                      />
                      <span style={{ color: securityColorCSS(result.system.sec), fontSize: 9 }}>
                        {result.system.sec.toFixed(1)}
                      </span>
                    </span>
                  </div>
                  {result.distanceLY !== undefined && (
                    <div style={{ fontSize: 8, color: '#3a3a3a', marginTop: 1 }}>
                      {result.distanceLY.toFixed(1)} LY from {result.characterName}
                      {result.system.hasStation && <span style={{ color: '#33aa55', marginLeft: 4 }}>STN</span>}
                    </div>
                  )}
                </div>
              ) : (
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{
                      fontSize: 8,
                      color: result.type === 'region' ? '#c8a951' : '#6688aa',
                      letterSpacing: '0.12em',
                      minWidth: 28,
                    }}>
                      {result.type === 'region' ? 'RGN' : 'CON'}
                    </span>
                    <span style={{ color: '#dedede' }}>{result.name}</span>
                  </span>
                  <span style={{ fontSize: 9, color: '#474747' }}>
                    {result.systemCount} sys · {result.secRange}
                  </span>
                </div>
              )}
            </div>
          ))}
          {hasMore && (
            <div style={{
              padding: '5px 10px',
              fontSize: 9,
              fontFamily: FONT,
              color: '#3a3a3a',
              letterSpacing: '0.1em',
              textAlign: 'center',
            }}>
              ... AND {totalCount - MAX_RESULTS} MORE
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Route action buttons (origin / dest / waypoint / avoid) ──── */

function RouteActionGroup({
  systemId,
  onSetRouteOrigin,
  onSetRouteDest,
  onAddRouteWaypoint,
  onAvoidSystem,
}: {
  systemId: number;
  onSetRouteOrigin?: (id: number) => void;
  onSetRouteDest?: (id: number) => void;
  onAddRouteWaypoint?: (id: number) => void;
  onAvoidSystem?: (id: number) => void;
}) {
  // Render nothing if no callbacks were provided.
  if (!onSetRouteOrigin && !onSetRouteDest && !onAddRouteWaypoint && !onAvoidSystem) {
    return null;
  }
  return (
    <span style={{ display: 'flex', gap: 2 }} onMouseDown={(e) => e.stopPropagation()}>
      {onSetRouteOrigin && (
        <RouteActionBtn
          title="Set as gate route origin"
          color="#33aa55"
          onClick={() => onSetRouteOrigin(systemId)}
        >
          O
        </RouteActionBtn>
      )}
      {onSetRouteDest && (
        <RouteActionBtn
          title="Set as gate route destination"
          color="#cc5533"
          onClick={() => onSetRouteDest(systemId)}
        >
          D
        </RouteActionBtn>
      )}
      {onAddRouteWaypoint && (
        <RouteActionBtn
          title="Add as gate route waypoint"
          color="#00d4ff"
          onClick={() => onAddRouteWaypoint(systemId)}
        >
          W
        </RouteActionBtn>
      )}
      {onAvoidSystem && (
        <RouteActionBtn
          title="Avoid this system"
          color="#cc3333"
          onClick={() => onAvoidSystem(systemId)}
        >
          ×
        </RouteActionBtn>
      )}
    </span>
  );
}

function RouteActionBtn({
  children,
  title,
  color,
  onClick,
}: {
  children: string;
  title: string;
  color: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      onMouseDown={(e) => {
        // Prevent the parent row's onMouseDown from firing.
        e.preventDefault();
        e.stopPropagation();
        onClick();
      }}
      style={{
        background: 'transparent',
        border: `1px solid ${color}`,
        color,
        cursor: 'pointer',
        fontSize: 8,
        fontFamily: FONT,
        lineHeight: 1,
        padding: '1px 4px',
        letterSpacing: '0.05em',
      }}
    >
      {children}
    </button>
  );
}
