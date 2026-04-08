import { useState, useMemo } from 'react';
import type { SystemData } from '../types';
import type { CharacterLocation } from '../useCharacterLocations';
import { securityColorCSS } from '../utils/colors';
import { canLightCyno } from '../jump/distance';
import { FONT, BG, BORDER, TEXT, MUTED, ACCENT } from './plannerStyles';

/**
 * Reusable system slot used by both the Jump Planner and Gate Route Planner.
 * Shows the currently-selected system, or a search input + character buttons
 * when no system is selected.
 *
 * `cynoOnly` (default false) — if true, restricts selectable systems to those
 * that can light a cyno (used by the jump planner destination slot).
 */
interface Props {
  systemId: number | null;
  systems: SystemData[];
  systemName: (id: number) => string;
  characters: CharacterLocation[];
  onSelect: (id: number) => void;
  onClear: () => void;
  onFocusSystem: (system: SystemData) => void;
  placeholder: string;
  /** Skip the cyno-capable filter on the search results (default for gate routing). */
  isOrigin?: boolean;
  /** Restrict search results to cyno-capable systems (jump planner destination). */
  cynoOnly?: boolean;
}

export function SystemSlotWithSearch({
  systemId,
  systems,
  systemName,
  characters,
  onSelect,
  onClear,
  onFocusSystem,
  placeholder,
  isOrigin,
  cynoOnly,
}: Props) {
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
            const disabled = cynoOnly && !isOrigin && !isCyno;
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
