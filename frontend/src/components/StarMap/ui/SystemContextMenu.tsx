import { useEffect, useRef } from 'react';
import type { SystemData } from '../types';
import { securityColorCSS } from '../utils/colors';
import { FONT, BG, BORDER, TEXT, MUTED } from './plannerStyles';

interface Props {
  system: SystemData;
  position: { x: number; y: number };
  onClose: () => void;
  onSetOrigin: (id: number) => void;
  onSetDestination: (id: number) => void;
  onAddWaypoint: (id: number) => void;
  onAvoidSystem: (id: number) => void;
}

/**
 * Right-click context menu for systems on the map. Triggered by the
 * `contextmenu` DOM event on the canvas. Closes on outside click or Escape.
 */
export function SystemContextMenu({
  system,
  position,
  onClose,
  onSetOrigin,
  onSetDestination,
  onAddWaypoint,
  onAvoidSystem,
}: Props) {
  const menuRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    // Defer to avoid closing on the same click that opened the menu
    const t = window.setTimeout(() => {
      document.addEventListener('click', handleClick);
      document.addEventListener('contextmenu', handleClick);
      document.addEventListener('keydown', handleKey);
    }, 0);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener('click', handleClick);
      document.removeEventListener('contextmenu', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [onClose]);

  // Clamp position to viewport so the menu doesn't overflow
  const left = Math.min(position.x, window.innerWidth - 200);
  const top = Math.min(position.y, window.innerHeight - 220);

  const item = (label: string, onClick: () => void, color: string = TEXT, divider?: boolean) => (
    <button
      key={label}
      onClick={() => { onClick(); onClose(); }}
      style={{
        display: 'block', width: '100%', textAlign: 'left',
        padding: '5px 10px', fontSize: 10, fontFamily: FONT,
        background: 'none', border: 'none', color, cursor: 'pointer',
        letterSpacing: '0.06em',
        borderTop: divider ? `1px solid ${BORDER}` : 'none',
      }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = 'rgba(255,255,255,0.04)'; }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = 'none'; }}
    >
      {label}
    </button>
  );

  return (
    <div
      ref={menuRef}
      style={{
        position: 'fixed',
        left,
        top,
        zIndex: 100,
        minWidth: 190,
        background: BG,
        border: `1px solid ${BORDER}`,
        fontFamily: FONT,
        boxShadow: '0 4px 12px rgba(0,0,0,0.6)',
      }}
    >
      <div style={{
        padding: '6px 10px', fontSize: 10, color: TEXT,
        borderBottom: `1px solid ${BORDER}`,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      }}>
        <span style={{ fontWeight: 600 }}>{system.name}</span>
        <span style={{ fontSize: 9, color: securityColorCSS(system.sec) }}>
          {system.sec.toFixed(1)}
        </span>
      </div>
      <div style={{ fontSize: 8, color: MUTED, padding: '2px 10px 4px', letterSpacing: '0.08em' }}>
        {system.regName}
      </div>

      {item('Set as Origin', () => onSetOrigin(system.id), '#33aa55', true)}
      {item('Set as Destination', () => onSetDestination(system.id), '#cc5533')}
      {item('Add as Waypoint', () => onAddWaypoint(system.id), '#00d4ff')}
      {item('Avoid this System', () => onAvoidSystem(system.id), '#cc3333')}

      {item(
        'View on zKillboard ↗',
        () => window.open(`https://zkillboard.com/system/${system.id}/`, '_blank', 'noopener'),
        TEXT,
        true,
      )}
      {item(
        'View on DOTLAN ↗',
        () => window.open(
          `https://evemaps.dotlan.net/system/${encodeURIComponent(system.name)}`,
          '_blank',
          'noopener',
        ),
        TEXT,
      )}
    </div>
  );
}
