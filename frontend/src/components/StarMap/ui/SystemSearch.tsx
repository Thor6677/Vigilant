import { useState, useRef, useMemo, useCallback, useEffect } from 'react';
import type { SystemData } from '../types';
import { securityColorCSS } from '../utils/colors';

const FONT = "'JetBrains Mono', monospace";

interface Props {
  systems: SystemData[];
  onSelect: (system: SystemData) => void;
}

export function SystemSearch({ systems, onSelect }: Props) {
  const [query, setQuery] = useState('');
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const sortedSystems = useMemo(() => {
    return [...systems].sort((a, b) => a.name.localeCompare(b.name));
  }, [systems]);

  const results = useMemo(() => {
    if (!query || query.length < 2) return [];
    const q = query.toLowerCase();
    const matches: SystemData[] = [];
    for (const sys of sortedSystems) {
      if (sys.name.toLowerCase().includes(q)) {
        matches.push(sys);
        if (matches.length >= 15) break;
      }
    }
    return matches;
  }, [query, sortedSystems]);

  const handleSelect = useCallback((sys: SystemData) => {
    setQuery('');
    setFocused(false);
    inputRef.current?.blur();
    onSelect(sys);
  }, [onSelect]);

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

  return (
    <div style={{
      position: 'absolute',
      top: 10,
      left: 10,
      zIndex: 30,
      width: 260,
    }}>
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={e => setQuery(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setTimeout(() => setFocused(false), 150)}
        onKeyDown={handleKeyDown}
        placeholder="SEARCH SYSTEMS (F)"
        aria-label="Search solar systems"
        style={{
          width: '100%',
          padding: '7px 10px',
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

      {showDropdown && (
        <div style={{
          background: 'rgba(14, 14, 14, 0.97)',
          border: '1px solid #191919',
          borderTop: 'none',
          maxHeight: 280,
          overflowY: 'auto',
        }}>
          {results.map((sys, i) => (
            <div
              key={sys.id}
              onMouseDown={() => handleSelect(sys)}
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
              }}
            >
              <span style={{ color: '#dedede' }}>{sys.name}</span>
              <span style={{ fontSize: 9, color: '#474747' }}>
                <span style={{ color: securityColorCSS(sys.sec), marginRight: 6 }}>
                  {sys.sec.toFixed(1)}
                </span>
                {sys.regName}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
