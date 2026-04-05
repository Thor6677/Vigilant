import type { GroupMode } from '../types';

const FONT = "'JetBrains Mono', monospace";

const MODES: { key: GroupMode; label: string }[] = [
  { key: 'systems', label: 'Systems' },
  { key: 'constellation', label: 'Constellation' },
  { key: 'region', label: 'Region' },
];

interface Props {
  mode: GroupMode;
  onModeChange: (mode: GroupMode) => void;
}

export function GroupModeControls({ mode, onModeChange }: Props) {
  return (
    <div style={{
      position: 'absolute',
      top: 10,
      right: 12,
      zIndex: 30,
      display: 'flex',
      gap: 0,
      background: 'rgba(14, 14, 14, 0.9)',
      border: '1px solid #191919',
    }}>
      <span style={{
        padding: '5px 8px',
        fontSize: 8,
        letterSpacing: '0.12em',
        fontFamily: FONT,
        color: '#3a3a3a',
        textTransform: 'uppercase',
        borderRight: '1px solid #191919',
        display: 'flex',
        alignItems: 'center',
      }}>
        GROUP
      </span>
      {MODES.map(({ key, label }) => (
        <button
          key={key}
          onClick={() => onModeChange(key)}
          style={{
            padding: '5px 10px',
            fontSize: 9,
            letterSpacing: '0.1em',
            textTransform: 'uppercase',
            fontFamily: FONT,
            background: mode === key ? '#0e0e0e' : 'transparent',
            color: mode === key ? '#c8a951' : '#474747',
            border: 'none',
            borderLeft: '1px solid #191919',
            cursor: 'pointer',
            whiteSpace: 'nowrap',
          }}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
