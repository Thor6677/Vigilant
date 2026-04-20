import type { OverlayType, IndustryIndexKind } from '../types';
import type { SovTimeRange } from '../useSovChanges';

const OVERLAYS: { key: OverlayType; label: string }[] = [
  { key: 'security',        label: 'Security' },
  { key: 'jumps',           label: 'Jumps' },
  { key: 'shipKills',       label: 'Ship Kills' },
  { key: 'podKills',        label: 'Pod Kills' },
  { key: 'npcKills',        label: 'NPC Kills' },
  { key: 'sovereignty',     label: 'Sovereignty' },
  { key: 'adm',             label: 'ADM' },
  { key: 'factionWarfare',  label: 'FW' },
  { key: 'incursions',      label: 'Incursions' },
  { key: 'industry',        label: 'Industry' },
  { key: 'planetType',      label: 'Planets' },
  { key: 'radar',           label: 'Radar' },
];

const INDUSTRY_KINDS: { key: IndustryIndexKind; label: string }[] = [
  { key: 'manufacturing', label: 'MFG' },
  { key: 'me',            label: 'ME' },
  { key: 'te',            label: 'TE' },
  { key: 'copying',       label: 'COPY' },
  { key: 'invention',     label: 'INV' },
  { key: 'reaction',      label: 'RXN' },
];

const SOV_RANGES: { key: SovTimeRange; label: string }[] = [
  { key: '24h', label: '24H' },
  { key: '7d',  label: '7D' },
  { key: '1m',  label: '1M' },
  { key: '6m',  label: '6M' },
  { key: '1y',  label: '1Y' },
];

interface Props {
  activeOverlay: OverlayType;
  onOverlayChange: (overlay: OverlayType) => void;
  statsLoaded: boolean;
  sovTimeRange?: SovTimeRange | null;
  onSovTimeRangeChange?: (range: SovTimeRange | null) => void;
  sovChangesCount?: number;
  sovChangesLoading?: boolean;
  isMobile?: boolean;
  industryKind?: IndustryIndexKind;
  onIndustryKindChange?: (k: IndustryIndexKind) => void;
  radarPivotName?: string | null;
  radarJumps?: number;
  onRadarJumpsChange?: (n: number) => void;
  onRadarClear?: () => void;
}

const FONT = "'JetBrains Mono', monospace";

export function OverlayControls({
  activeOverlay, onOverlayChange, statsLoaded,
  sovTimeRange, onSovTimeRangeChange, sovChangesCount = 0, sovChangesLoading = false,
  isMobile,
  industryKind = 'manufacturing',
  onIndustryKindChange,
  radarPivotName,
  radarJumps = 3,
  onRadarJumpsChange,
  onRadarClear,
}: Props) {
  return (
    <div style={{ fontFamily: FONT }}>
      <div style={{
        display: 'flex',
        alignItems: 'stretch',
        background: '#080808',
        borderTop: '1px solid #191919',
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
                  padding: isMobile ? '10px 10px' : '8px 12px',
                  fontSize: isMobile ? 10 : 9,
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

        {/* Color legend — hidden on mobile */}
        <div style={{
          display: isMobile ? 'none' : 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '0 12px',
          fontSize: 8,
          color: '#474747',
          letterSpacing: '0.1em',
          flexShrink: 0,
        }}>
          {activeOverlay === 'security' && <SecurityLegend />}
          {['jumps', 'shipKills', 'podKills', 'npcKills', 'industry'].includes(activeOverlay) && <HeatmapLegend />}
          {activeOverlay === 'sovereignty' && <SovLegend hasChanges={sovTimeRange != null && sovChangesCount > 0} />}
          {activeOverlay === 'adm' && <ADMLegend />}
          {activeOverlay === 'factionWarfare' && <FWLegend />}
          {activeOverlay === 'incursions' && <IncursionLegend />}
          {activeOverlay === 'planetType' && <PlanetLegend />}
          {activeOverlay === 'radar' && <RadarLegend />}
        </div>
      </div>

      {/* Industry index-kind sub-bar */}
      {activeOverlay === 'industry' && (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 0,
          background: '#060606',
          borderTop: '1px solid #131313',
          padding: '0 4px',
        }}>
          <span style={{
            padding: '6px 8px',
            fontSize: 8,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            color: '#333',
          }}>
            Activity
          </span>
          {INDUSTRY_KINDS.map(({ key, label }) => {
            const isActive = industryKind === key;
            return (
              <button
                key={key}
                onClick={() => onIndustryKindChange?.(key)}
                style={{
                  padding: '6px 10px',
                  fontSize: 8,
                  letterSpacing: '0.12em',
                  textTransform: 'uppercase',
                  fontFamily: FONT,
                  background: isActive ? '#0e0e0e' : 'transparent',
                  color: isActive ? '#c8a951' : '#474747',
                  border: 'none',
                  borderTop: isActive ? '1px solid #c8a951' : '1px solid transparent',
                  cursor: 'pointer',
                  transition: 'color 0.15s',
                }}
              >
                {label}
              </button>
            );
          })}
        </div>
      )}

      {/* Radar mode sub-bar */}
      {activeOverlay === 'radar' && (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          background: '#060606',
          borderTop: '1px solid #131313',
          padding: '4px 8px',
        }}>
          <span style={{
            fontSize: 8,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            color: '#333',
          }}>
            Pivot
          </span>
          <span style={{
            fontSize: 10,
            color: radarPivotName ? '#c8a951' : '#474747',
            letterSpacing: '0.05em',
            marginRight: 8,
          }}>
            {radarPivotName || 'right-click a system · "Radar Pivot"'}
          </span>
          <span style={{
            fontSize: 8,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            color: '#333',
          }}>
            Reach
          </span>
          {[1, 2, 3, 4, 5].map(n => {
            const isActive = radarJumps === n;
            return (
              <button
                key={n}
                onClick={() => onRadarJumpsChange?.(n)}
                style={{
                  padding: '4px 10px',
                  fontSize: 9,
                  fontFamily: FONT,
                  background: isActive ? '#0e0e0e' : 'transparent',
                  color: isActive ? '#c8a951' : '#474747',
                  border: '1px solid ' + (isActive ? '#c8a951' : '#191919'),
                  cursor: 'pointer',
                  transition: 'color 0.15s',
                }}
              >
                {n}
              </button>
            );
          })}
          {radarPivotName && (
            <button
              onClick={onRadarClear}
              style={{
                marginLeft: 'auto',
                padding: '4px 8px',
                fontSize: 8,
                letterSpacing: '0.12em',
                fontFamily: FONT,
                background: 'transparent',
                color: '#888',
                border: '1px solid #191919',
                cursor: 'pointer',
              }}
            >
              CLEAR
            </button>
          )}
        </div>
      )}

      {/* Sovereignty time range sub-bar */}
      {activeOverlay === 'sovereignty' && (
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 0,
          background: '#060606',
          borderTop: '1px solid #131313',
          padding: '0 4px',
        }}>
          <button
            onClick={() => onSovTimeRangeChange?.(null)}
            style={{
              padding: '6px 10px',
              fontSize: 8,
              letterSpacing: '0.12em',
              textTransform: 'uppercase',
              fontFamily: FONT,
              background: !sovTimeRange ? '#0e0e0e' : 'transparent',
              color: !sovTimeRange ? '#c8a951' : '#474747',
              border: 'none',
              borderTop: !sovTimeRange ? '1px solid #c8a951' : '1px solid transparent',
              cursor: 'pointer',
              transition: 'color 0.15s',
            }}
          >
            Now
          </button>
          <span style={{
            padding: '6px 4px',
            fontSize: 8,
            color: '#222',
          }}>
            |
          </span>
          <span style={{
            padding: '6px 10px',
            fontSize: 8,
            letterSpacing: '0.12em',
            textTransform: 'uppercase',
            color: '#333',
          }}>
            Changes
          </span>
          {SOV_RANGES.map(({ key, label }) => {
            const isActive = sovTimeRange === key;
            return (
              <button
                key={key}
                onClick={() => onSovTimeRangeChange?.(key)}
                style={{
                  padding: '6px 10px',
                  fontSize: 8,
                  letterSpacing: '0.12em',
                  textTransform: 'uppercase',
                  fontFamily: FONT,
                  background: isActive ? '#0e0e0e' : 'transparent',
                  color: isActive ? '#c8a951' : '#474747',
                  border: 'none',
                  borderTop: isActive ? '1px solid #c8a951' : '1px solid transparent',
                  cursor: 'pointer',
                  transition: 'color 0.15s',
                }}
              >
                {label}
              </button>
            );
          })}
          {sovChangesLoading && (
            <span style={{ fontSize: 8, color: '#333', marginLeft: 8 }}>loading...</span>
          )}
          {!sovChangesLoading && sovTimeRange && sovChangesCount > 0 && (
            <span style={{ fontSize: 8, color: '#c8a951', marginLeft: 8 }}>
              {sovChangesCount} system{sovChangesCount !== 1 ? 's' : ''}
            </span>
          )}
          {!sovChangesLoading && sovTimeRange && sovChangesCount === 0 && (
            <span style={{ fontSize: 8, color: '#333', marginLeft: 8 }}>no changes</span>
          )}
        </div>
      )}
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

function SovLegend({ hasChanges }: { hasChanges?: boolean }) {
  return (
    <>
      <Dot color="#555577" label="NPC" />
      <Dot color="#44aaee" label="PLAYER" />
      {hasChanges && <Dot color="#ffffff" label="CHANGED" />}
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

function ADMLegend() {
  return (
    <>
      <span>WEAK</span>
      <span style={{
        width: 50, height: 6, display: 'inline-block',
        background: 'linear-gradient(to right, #cc3333, #ccaa33, #33aa66)',
      }} />
      <span>STRONG</span>
    </>
  );
}

function PlanetLegend() {
  return (
    <>
      <Dot color="#33aa66" label="TEMP" />
      <Dot color="#88ccee" label="ICE" />
      <Dot color="#66aa99" label="GAS" />
      <Dot color="#3366cc" label="OCN" />
      <Dot color="#ee3300" label="LAVA" />
      <Dot color="#a08060" label="BARR" />
      <Dot color="#bb66cc" label="STRM" />
      <Dot color="#ee8844" label="PLSM" />
    </>
  );
}

function RadarLegend() {
  return (
    <>
      <Dot color="#c8a951" label="PIVOT" />
      <Dot color="#48f148" label="1J" />
      <Dot color="#ccaa33" label="2J" />
      <Dot color="#ef6f00" label="3J+" />
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
