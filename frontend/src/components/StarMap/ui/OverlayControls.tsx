import type { OverlayType } from '../types';

const OVERLAYS: { key: OverlayType; label: string }[] = [
  { key: 'security',        label: 'Security' },
  { key: 'jumps',           label: 'Jumps' },
  { key: 'shipKills',       label: 'Ship Kills' },
  { key: 'podKills',        label: 'Pod Kills' },
  { key: 'npcKills',        label: 'NPC Kills' },
  { key: 'sovereignty',     label: 'Sovereignty' },
  { key: 'factionWarfare',  label: 'FW' },
  { key: 'incursions',      label: 'Incursions' },
];

interface Props {
  activeOverlay: OverlayType;
  onOverlayChange: (overlay: OverlayType) => void;
  statsLoaded: boolean;
}

const FONT = "'JetBrains Mono', monospace";

export function OverlayControls({ activeOverlay, onOverlayChange, statsLoaded }: Props) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'stretch',
      background: '#080808',
      borderTop: '1px solid #191919',
      fontFamily: FONT,
    }}>
      {/* Overlay selector buttons */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 0,
        overflowX: 'auto',
        flex: 1,
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
                padding: '8px 12px',
                fontSize: 9,
                letterSpacing: '0.12em',
                textTransform: 'uppercase',
                fontFamily: FONT,
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
              {isDisabled && (
                <span style={{ marginLeft: 4, opacity: 0.3 }}>·</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Color legend */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '0 12px',
        fontSize: 8,
        color: '#474747',
        letterSpacing: '0.1em',
        flexShrink: 0,
      }}>
        {activeOverlay === 'security' && <SecurityLegend />}
        {['jumps', 'shipKills', 'podKills', 'npcKills'].includes(activeOverlay) && <HeatmapLegend />}
        {activeOverlay === 'sovereignty' && <SovLegend />}
        {activeOverlay === 'factionWarfare' && <FWLegend />}
        {activeOverlay === 'incursions' && <IncursionLegend />}
      </div>
    </div>
  );
}

function SecurityLegend() {
  const stops = [
    { sec: '1.0', color: '#2fefef' },
    { sec: '0.5', color: '#efef00' },
    { sec: '0.0', color: '#f05050' },
    { sec: '-1', color: '#8f0000' },
  ];
  return (
    <>
      {stops.map(({ sec, color }) => (
        <span key={sec} style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          <span style={{ width: 6, height: 6, background: color, display: 'inline-block' }} />
          <span>{sec}</span>
        </span>
      ))}
    </>
  );
}

function HeatmapLegend() {
  return (
    <>
      <span>LOW</span>
      <span style={{
        width: 50, height: 6, display: 'inline-block',
        background: 'linear-gradient(to right, #1a1a40, #efef00, #ef0000)',
      }} />
      <span>HIGH</span>
    </>
  );
}

function SovLegend() {
  return (
    <>
      <Dot color="#555577" label="NPC" />
      <Dot color="#44aaee" label="PLAYER" />
    </>
  );
}

function FWLegend() {
  return (
    <>
      <Dot color="#4488cc" label="CAL" />
      <Dot color="#cc6633" label="MIN" />
      <Dot color="#ccaa33" label="AMA" />
      <Dot color="#33aa66" label="GAL" />
    </>
  );
}

function IncursionLegend() {
  return (
    <>
      <Dot color="#ff4444" label="INFESTED" />
      <Dot color="#ff8800" label="STAGING" />
    </>
  );
}

function Dot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
      <span style={{ width: 6, height: 6, background: color, display: 'inline-block' }} />
      <span>{label}</span>
    </span>
  );
}
