import type { OverlayType } from '../types';

const OVERLAYS: { key: OverlayType; label: string; icon: string }[] = [
  { key: 'security',  label: 'Security',   icon: 'S' },
  { key: 'jumps',     label: 'Jumps',      icon: 'J' },
  { key: 'shipKills', label: 'Ship Kills', icon: 'SK' },
  { key: 'podKills',  label: 'Pod Kills',  icon: 'PK' },
  { key: 'npcKills',  label: 'NPC Kills',  icon: 'NK' },
  { key: 'sovereignty', label: 'Sovereignty', icon: 'SO' },
];

interface Props {
  activeOverlay: OverlayType;
  onOverlayChange: (overlay: OverlayType) => void;
  statsLoaded: boolean;
}

export function OverlayControls({ activeOverlay, onOverlayChange, statsLoaded }: Props) {
  return (
    <div style={{
      position: 'absolute',
      bottom: 0,
      left: 0,
      right: 0,
      display: 'flex',
      alignItems: 'stretch',
      background: '#080808',
      borderTop: '1px solid #191919',
      zIndex: 30,
      fontFamily: "'JetBrains Mono', monospace",
    }}>
      {/* Overlay selector buttons */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 0,
        overflow: 'auto',
      }}>
        {OVERLAYS.map(({ key, label }) => {
          const isActive = activeOverlay === key;
          const isDisabled = key !== 'security' && !statsLoaded;
          return (
            <button
              key={key}
              onClick={() => !isDisabled && onOverlayChange(key)}
              disabled={isDisabled}
              style={{
                padding: '8px 14px',
                fontSize: 10,
                letterSpacing: '0.12em',
                textTransform: 'uppercase',
                fontFamily: "'JetBrains Mono', monospace",
                background: isActive ? '#0e0e0e' : 'transparent',
                color: isActive ? '#c8a951' : isDisabled ? '#2a2a2a' : '#474747',
                border: 'none',
                borderTop: isActive ? '2px solid #c8a951' : '2px solid transparent',
                cursor: isDisabled ? 'default' : 'pointer',
                whiteSpace: 'nowrap',
                transition: 'color 0.15s',
              }}
              title={isDisabled ? 'Loading stats...' : label}
            >
              {label}
            </button>
          );
        })}
      </div>

      {/* Color legend */}
      <div style={{
        marginLeft: 'auto',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '0 16px',
        fontSize: 9,
        color: '#474747',
        letterSpacing: '0.1em',
      }}>
        {activeOverlay === 'security' && <SecurityLegend />}
        {(activeOverlay === 'jumps' || activeOverlay === 'shipKills' || activeOverlay === 'podKills' || activeOverlay === 'npcKills') && <HeatmapLegend />}
        {activeOverlay === 'sovereignty' && <SovLegend />}
      </div>
    </div>
  );
}

function SecurityLegend() {
  const stops = [
    { sec: '1.0', color: '#2fefef' },
    { sec: '0.7', color: '#00ef47' },
    { sec: '0.5', color: '#efef00' },
    { sec: '0.3', color: '#ef6f00' },
    { sec: '0.0', color: '#f05050' },
    { sec: '-1.0', color: '#8f0000' },
  ];
  return (
    <>
      <span style={{ textTransform: 'uppercase' }}>Sec Status</span>
      {stops.map(({ sec, color }) => (
        <span key={sec} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
          <span style={{ width: 8, height: 8, background: color, display: 'inline-block' }} />
          <span>{sec}</span>
        </span>
      ))}
    </>
  );
}

function HeatmapLegend() {
  return (
    <>
      <span style={{ textTransform: 'uppercase' }}>Activity</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
        <span style={{ width: 8, height: 8, background: '#1a1a40', display: 'inline-block' }} />
        <span>Low</span>
      </span>
      <span style={{
        width: 60, height: 8, display: 'inline-block',
        background: 'linear-gradient(to right, #1a1a40, #efef00, #ef0000)',
      }} />
      <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
        <span style={{ width: 8, height: 8, background: '#ef0000', display: 'inline-block' }} />
        <span>High</span>
      </span>
    </>
  );
}

function SovLegend() {
  return (
    <>
      <span style={{ textTransform: 'uppercase' }}>Sov</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
        <span style={{ width: 8, height: 8, background: '#555577', display: 'inline-block' }} />
        <span>NPC</span>
      </span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
        <span style={{ width: 8, height: 8, background: '#44aaee', display: 'inline-block' }} />
        <span>Player</span>
      </span>
    </>
  );
}
