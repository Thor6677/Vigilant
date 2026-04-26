import type { OverlayType, IndustryIndexKind, PlanetTypeId, KillHeatmapWindow } from '../types';
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
  { key: 'killHeatmap',     label: 'Kill Heatmap' },
];

const KILL_WINDOWS: { key: KillHeatmapWindow; label: string }[] = [
  { key: '1d',  label: '1D' },
  { key: '7d',  label: '7D' },
  { key: '30d', label: '30D' },
];

const INDUSTRY_KINDS: { key: IndustryIndexKind; label: string }[] = [
  { key: 'manufacturing', label: 'MFG' },
  { key: 'me',            label: 'ME' },
  { key: 'te',            label: 'TE' },
  { key: 'copying',       label: 'COPY' },
  { key: 'invention',     label: 'INV' },
  { key: 'reaction',      label: 'RXN' },
];

const PLANET_KINDS: { key: PlanetTypeId | null; label: string; color: string }[] = [
  { key: null,   label: 'ALL',  color: '#c8a951' },
  { key: 11,     label: 'TEMP', color: '#33aa66' },
  { key: 12,     label: 'ICE',  color: '#88ccee' },
  { key: 13,     label: 'GAS',  color: '#66aa99' },
  { key: 2014,   label: 'OCN',  color: '#3366cc' },
  { key: 2015,   label: 'LAVA', color: '#ee3300' },
  { key: 2016,   label: 'BARR', color: '#a08060' },
  { key: 2017,   label: 'STRM', color: '#bb66cc' },
  { key: 2063,   label: 'PLSM', color: '#ee8844' },
  { key: 30889,  label: 'SHAT', color: '#888888' },
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
  planetKind?: PlanetTypeId | null;
  onPlanetKindChange?: (k: PlanetTypeId | null) => void;
  radarPivotName?: string | null;
  radarJumps?: number;
  onRadarJumpsChange?: (n: number) => void;
  onRadarClear?: () => void;
  killHeatmapWindow?: KillHeatmapWindow;
  onKillHeatmapWindowChange?: (w: KillHeatmapWindow) => void;
  killHeatmapBuckets?: string[];
  killHeatmapBucketIdx?: number;
  onKillHeatmapBucketIdxChange?: (i: number) => void;
  killHeatmapPlaying?: boolean;
  onKillHeatmapPlayPauseToggle?: () => void;
  killHeatmapLoading?: boolean;
  killHeatmapMaxValue?: number;
}

const FONT = "'JetBrains Mono', monospace";

export function OverlayControls({
  activeOverlay, onOverlayChange, statsLoaded,
  sovTimeRange, onSovTimeRangeChange, sovChangesCount = 0, sovChangesLoading = false,
  isMobile,
  industryKind = 'manufacturing',
  onIndustryKindChange,
  planetKind = null,
  onPlanetKindChange,
  radarPivotName,
  radarJumps = 3,
  onRadarJumpsChange,
  killHeatmapWindow = '1d',
  onKillHeatmapWindowChange,
  killHeatmapBuckets = [],
  killHeatmapBucketIdx = 0,
  onKillHeatmapBucketIdxChange,
  killHeatmapPlaying = false,
  onKillHeatmapPlayPauseToggle,
  killHeatmapLoading = false,
  killHeatmapMaxValue = 0,
  onRadarClear,
}: Props) {
  // ── Sub-bar renderers ──────────────────────────────────────────────────
  // Each sub-bar now renders ABOVE the main overlay row so popups grow toward
  // the map instead of into the screen edge. Visual separators live on the
  // BOTTOM of each sub-bar (plus the main row's borderTop) so the boundary
  // between sub-bar and main row stays crisp.

  const subBarStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: 0,
    background: '#060606',
    borderTop: '1px solid #191919',
    borderBottom: '1px solid #131313',
    padding: '0 4px',
  };

  const industrySubBar = activeOverlay === 'industry' ? (
    <div style={subBarStyle}>
      <span style={{
        padding: '6px 8px', fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: '#333',
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
              padding: '6px 10px', fontSize: 8, letterSpacing: '0.12em',
              textTransform: 'uppercase', fontFamily: FONT,
              background: isActive ? '#0e0e0e' : 'transparent',
              color: isActive ? '#c8a951' : '#474747',
              border: 'none',
              borderBottom: isActive ? '1px solid #c8a951' : '1px solid transparent',
              cursor: 'pointer', transition: 'color 0.15s',
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  ) : null;

  const planetSubBar = activeOverlay === 'planetType' ? (
    <div style={{ ...subBarStyle, gap: 0, overflowX: 'auto' }}>
      <span style={{
        padding: '6px 8px', fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: '#333', whiteSpace: 'nowrap',
      }}>
        Planet
      </span>
      {PLANET_KINDS.map(({ key, label, color }) => {
        const isActive = planetKind === key;
        return (
          <button
            key={String(key)}
            onClick={() => onPlanetKindChange?.(key)}
            style={{
              padding: '6px 10px', fontSize: 8, letterSpacing: '0.12em',
              textTransform: 'uppercase', fontFamily: FONT,
              background: isActive ? '#0e0e0e' : 'transparent',
              color: isActive ? color : '#474747',
              border: 'none',
              borderBottom: isActive ? `1px solid ${color}` : '1px solid transparent',
              cursor: 'pointer', transition: 'color 0.15s',
              whiteSpace: 'nowrap',
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  ) : null;

  const radarSubBar = activeOverlay === 'radar' ? (
    <div style={{ ...subBarStyle, gap: 6, padding: '4px 8px' }}>
      <span style={{
        fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: '#333',
      }}>
        Pivot
      </span>
      <span style={{
        fontSize: 10, color: radarPivotName ? '#c8a951' : '#474747',
        letterSpacing: '0.05em', marginRight: 8,
      }}>
        {radarPivotName || 'right-click a system · "Radar Pivot"'}
      </span>
      <span style={{
        fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: '#333',
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
              padding: '4px 10px', fontSize: 9, fontFamily: FONT,
              background: isActive ? '#0e0e0e' : 'transparent',
              color: isActive ? '#c8a951' : '#474747',
              border: '1px solid ' + (isActive ? '#c8a951' : '#191919'),
              cursor: 'pointer', transition: 'color 0.15s',
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
            marginLeft: 'auto', padding: '4px 8px', fontSize: 8,
            letterSpacing: '0.12em', fontFamily: FONT,
            background: 'transparent', color: '#888',
            border: '1px solid #191919', cursor: 'pointer',
          }}
        >
          CLEAR
        </button>
      )}
    </div>
  ) : null;

  const sovSubBar = activeOverlay === 'sovereignty' ? (
    <div style={subBarStyle}>
      <button
        onClick={() => onSovTimeRangeChange?.(null)}
        style={{
          padding: '6px 10px', fontSize: 8, letterSpacing: '0.12em',
          textTransform: 'uppercase', fontFamily: FONT,
          background: !sovTimeRange ? '#0e0e0e' : 'transparent',
          color: !sovTimeRange ? '#c8a951' : '#474747',
          border: 'none',
          borderBottom: !sovTimeRange ? '1px solid #c8a951' : '1px solid transparent',
          cursor: 'pointer', transition: 'color 0.15s',
        }}
      >
        Now
      </button>
      <span style={{ padding: '6px 4px', fontSize: 8, color: '#222' }}>|</span>
      <span style={{
        padding: '6px 10px', fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: '#333',
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
              padding: '6px 10px', fontSize: 8, letterSpacing: '0.12em',
              textTransform: 'uppercase', fontFamily: FONT,
              background: isActive ? '#0e0e0e' : 'transparent',
              color: isActive ? '#c8a951' : '#474747',
              border: 'none',
              borderBottom: isActive ? '1px solid #c8a951' : '1px solid transparent',
              cursor: 'pointer', transition: 'color 0.15s',
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
  ) : null;

  const fmtBucket = (iso: string, win: KillHeatmapWindow): string => {
    if (!iso) return '';
    if (win === '1d') {
      // ISO with trailing Z; show "Apr 25 14:00 UTC"
      const d = new Date(iso);
      if (isNaN(d.valueOf())) return iso;
      const mon = d.toLocaleString('en-US', { month: 'short', timeZone: 'UTC' });
      const day = d.getUTCDate();
      const hr = String(d.getUTCHours()).padStart(2, '0');
      return `${mon} ${day} ${hr}:00 UTC`;
    }
    // Daily bucket — already a YYYY-MM-DD string from the server.
    return iso;
  };

  const killHeatmapSubBar = activeOverlay === 'killHeatmap' ? (
    <div style={{ ...subBarStyle, gap: 8, padding: '4px 8px', flexWrap: 'wrap' }}>
      <span style={{
        fontSize: 8, letterSpacing: '0.12em',
        textTransform: 'uppercase', color: '#333',
      }}>
        Window
      </span>
      {KILL_WINDOWS.map(({ key, label }) => {
        const isActive = killHeatmapWindow === key;
        return (
          <button
            key={key}
            onClick={() => onKillHeatmapWindowChange?.(key)}
            style={{
              padding: '4px 10px', fontSize: 9, fontFamily: FONT,
              background: isActive ? '#0e0e0e' : 'transparent',
              color: isActive ? '#c8a951' : '#474747',
              border: '1px solid ' + (isActive ? '#c8a951' : '#191919'),
              cursor: 'pointer', transition: 'color 0.15s',
            }}
          >
            {label}
          </button>
        );
      })}
      <button
        onClick={onKillHeatmapPlayPauseToggle}
        disabled={killHeatmapBuckets.length === 0}
        style={{
          padding: '4px 10px', fontSize: 9, fontFamily: FONT,
          background: killHeatmapPlaying ? '#0e0e0e' : 'transparent',
          color: killHeatmapPlaying ? '#c8a951' : '#888',
          border: '1px solid ' + (killHeatmapPlaying ? '#c8a951' : '#191919'),
          cursor: killHeatmapBuckets.length === 0 ? 'default' : 'pointer',
          marginLeft: 4,
        }}
      >
        {killHeatmapPlaying ? '❚❚' : '▶'}
      </button>
      <input
        type="range"
        min={0}
        max={Math.max(0, killHeatmapBuckets.length - 1)}
        step={1}
        value={killHeatmapBucketIdx}
        onChange={(e) => onKillHeatmapBucketIdxChange?.(parseInt(e.target.value, 10))}
        disabled={killHeatmapBuckets.length === 0}
        style={{ flex: 1, minWidth: 120, accentColor: '#c8a951' }}
      />
      <span style={{
        fontSize: 9, color: killHeatmapLoading ? '#444' : '#c8a951',
        fontFamily: FONT, minWidth: 130, textAlign: 'right',
        letterSpacing: '0.05em',
      }}>
        {killHeatmapLoading
          ? 'loading…'
          : (killHeatmapBuckets.length > 0
              ? fmtBucket(killHeatmapBuckets[killHeatmapBucketIdx] ?? '', killHeatmapWindow)
              : 'no data')}
      </span>
      {killHeatmapMaxValue > 0 && !killHeatmapLoading && (
        <span style={{
          fontSize: 8, letterSpacing: '0.12em', color: '#333',
          textTransform: 'uppercase',
        }}>
          peak {killHeatmapMaxValue}
        </span>
      )}
    </div>
  ) : null;

  return (
    <div style={{ fontFamily: FONT }}>
      {/* Sub-bars render ABOVE the main overlay row. Only one is ever active
          at a time based on activeOverlay. */}
      {industrySubBar}
      {planetSubBar}
      {radarSubBar}
      {sovSubBar}
      {killHeatmapSubBar}

      {/* Main overlay row — the persistent tab strip along the bottom. */}
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
          {['jumps', 'shipKills', 'podKills', 'npcKills', 'industry', 'killHeatmap'].includes(activeOverlay) && <HeatmapLegend />}
          {activeOverlay === 'sovereignty' && <SovLegend hasChanges={sovTimeRange != null && sovChangesCount > 0} />}
          {activeOverlay === 'adm' && <ADMLegend />}
          {activeOverlay === 'factionWarfare' && <FWLegend />}
          {activeOverlay === 'incursions' && <IncursionLegend />}
          {activeOverlay === 'planetType' && <PlanetLegend />}
          {activeOverlay === 'radar' && <RadarLegend />}
        </div>
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
