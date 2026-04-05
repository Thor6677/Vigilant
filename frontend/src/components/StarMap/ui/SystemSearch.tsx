import { useState, useRef, useMemo, useCallback, useEffect } from 'react';
import type { SystemData } from '../types';
import { securityColorCSS } from '../utils/colors';

interface Props {
  systems: SystemData[];
  onSelect: (system: SystemData) => void;
}

export function SystemSearch({ systems, onSelect }: Props) {
  const [query, setQuery] = useState('');
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Sorted names for fast prefix search
  const sortedSystems = useMemo(() => {
    return [...systems].sort((a, b) => a.name.localeCompare(b.name));
  }, [systems]);

  // Filter results
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

  // Keyboard navigation
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

  // Reset active index when results change
  useEffect(() => setActiveIndex(0), [results]);

  // Global keyboard shortcut: F or / to focus search
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
      top: 12,
      left: 12,
      zIndex: 30,
      width: 280,
    }}>
      <input
        ref={inputRef}
        type="text"
        value={query}
        onChange={e => setQuery(e.target.value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setTimeout(() => setFocused(false), 150)}
        onKeyDown={handleKeyDown}
        placeholder="Search systems... (F)"
        aria-label="Search solar systems"
        style={{
          width: '100%',
          padding: '8px 12px',
          fontSize: 14,
          fontFamily: 'DM Sans, sans-serif',
          background: 'rgba(10, 14, 30, 0.92)',
          color: '#C8D8E8',
          border: '1px solid #2a3a5a',
          borderRadius: showDropdown ? '6px 6px 0 0' : 6,
          outline: 'none',
        }}
      />

      {showDropdown && (
        <div style={{
          background: 'rgba(10, 14, 30, 0.95)',
          border: '1px solid #2a3a5a',
          borderTop: 'none',
          borderRadius: '0 0 6px 6px',
          maxHeight: 300,
          overflowY: 'auto',
        }}>
          {results.map((sys, i) => (
            <div
              key={sys.id}
              onMouseDown={() => handleSelect(sys)}
              style={{
                padding: '7px 12px',
                fontSize: 13,
                fontFamily: 'DM Sans, sans-serif',
                cursor: 'pointer',
                background: i === activeIndex ? 'rgba(42, 58, 90, 0.5)' : 'transparent',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
              }}
            >
              <span style={{ color: '#e0eaf4' }}>{sys.name}</span>
              <span style={{ fontSize: 11, color: '#6a7a8a' }}>
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
