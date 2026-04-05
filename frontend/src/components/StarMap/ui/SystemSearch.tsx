import { useState, useRef, useMemo, useCallback, useEffect } from 'react';
import type { SystemData } from '../types';
import { securityColorCSS } from '../utils/colors';

const FONT = "'JetBrains Mono', monospace";
const MAX_RESULTS = 15;

export type SearchResultType = 'system' | 'constellation' | 'region';

export interface SearchResult {
  type: SearchResultType;
  id: number;
  name: string;
  // System-specific
  system?: SystemData;
  // Area-specific (constellation/region)
  systemCount?: number;
  secRange?: string;
}

interface Props {
  systems: SystemData[];
  onSelectSystem: (system: SystemData) => void;
  onSelectArea: (type: 'constellation' | 'region', id: number, name: string) => void;
}

export function SystemSearch({ systems, onSelectSystem, onSelectArea }: Props) {
  const [query, setQuery] = useState('');
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Pre-compute unique regions and constellations
  const { regionList, constellationList } = useMemo(() => {
    const regMap = new Map<number, { name: string; count: number; minSec: number; maxSec: number }>();
    const conMap = new Map<number, { name: string; regName: string; count: number; minSec: number; maxSec: number }>();

    for (const sys of systems) {
      // Regions
      let r = regMap.get(sys.regId);
      if (!r) { r = { name: sys.regName, count: 0, minSec: sys.sec, maxSec: sys.sec }; regMap.set(sys.regId, r); }
      r.count++;
      r.minSec = Math.min(r.minSec, sys.sec);
      r.maxSec = Math.max(r.maxSec, sys.sec);

      // Constellations
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

  // Unified search results
  const { results, totalCount } = useMemo(() => {
    if (!query || query.length < 2) return { results: [] as SearchResult[], totalCount: 0 };
    const q = query.toLowerCase();
    const out: SearchResult[] = [];
    let total = 0;

    // 1. Matching regions (show first)
    for (const reg of regionList) {
      if (reg.name.toLowerCase().includes(q)) {
        total++;
        if (out.length < MAX_RESULTS) {
          out.push({
            type: 'region',
            id: reg.id,
            name: reg.name,
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
            type: 'constellation',
            id: con.id,
            name: con.name,
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
  }, [query, systems, regionList, constellationList]);

  const handleSelect = useCallback((result: SearchResult) => {
    setQuery('');
    setFocused(false);
    inputRef.current?.blur();

    if (result.type === 'system' && result.system) {
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
          placeholder="SEARCH SYSTEMS, REGIONS... (F)"
          aria-label="Search systems, regions, constellations"
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
              key={`${result.type}-${result.id}`}
              onMouseDown={() => handleSelect(result)}
              style={{
                padding: '6px 10px',
                fontSize: 10,
                fontFamily: FONT,
                cursor: 'pointer',
                background: i === activeIndex ? 'rgba(200,169,81,0.07)' : 'transparent',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                letterSpacing: '0.08em',
                borderTop: i > 0 && result.type !== results[i - 1].type ? '1px solid #191919' : 'none',
              }}
            >
              {result.type === 'system' && result.system ? (
                <>
                  <span style={{ color: '#dedede' }}>{result.name}</span>
                  <span style={{ fontSize: 9, color: '#474747' }}>
                    <span style={{ color: securityColorCSS(result.system.sec), marginRight: 6 }}>
                      {result.system.sec.toFixed(1)}
                    </span>
                    {result.system.regName}
                  </span>
                </>
              ) : (
                <>
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
                </>
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
