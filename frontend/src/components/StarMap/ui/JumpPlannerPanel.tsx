import type { JumpShipClass } from '../types';
import type { JumpPlannerState } from '../useJumpPlanner';
import { JUMP_SHIPS } from '../jump/constants';

const FONT = "'JetBrains Mono', monospace";
const BG = 'rgba(14, 14, 14, 0.97)';
const BORDER = '#191919';
const TEXT = '#dedede';
const MUTED = '#474747';
const ACCENT = '#c8a951';
const JUMP_COLOR = '#ff8800';

interface Props {
  planner: JumpPlannerState;
  systemName: (id: number) => string;
}

export function JumpPlannerPanel({ planner, systemName }: Props) {
  const shipEntries = Object.entries(JUMP_SHIPS) as [JumpShipClass, typeof JUMP_SHIPS[JumpShipClass]][];

  return (
    <div style={{
      position: 'absolute',
      top: 48,
      left: 10,
      width: 260,
      maxHeight: 'calc(100% - 100px)',
      overflowY: 'auto',
      background: BG,
      border: `1px solid ${BORDER}`,
      padding: '10px 12px',
      fontFamily: FONT,
      fontSize: 10,
      color: TEXT,
      zIndex: 30,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <span style={{ fontSize: 11, fontWeight: 600, letterSpacing: '0.1em', color: JUMP_COLOR }}>
          JUMP PLANNER
        </span>
        <button onClick={() => planner.setActive(false)} style={{
          background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
          fontSize: 14, fontFamily: FONT, lineHeight: 1,
        }}>×</button>
      </div>

      {/* Ship class */}
      <Label text="SHIP CLASS" />
      <select
        value={planner.shipClass}
        onChange={e => planner.setShipClass(e.target.value as JumpShipClass)}
        style={{
          width: '100%', padding: '4px 6px', fontSize: 10, fontFamily: FONT,
          background: '#080808', color: TEXT, border: `1px solid ${BORDER}`,
          cursor: 'pointer', marginBottom: 8,
        }}
      >
        {shipEntries.map(([key, cfg]) => (
          <option key={key} value={key}>{cfg.label}</option>
        ))}
      </select>

      {/* Skill levels */}
      <div style={{ display: 'flex', gap: 12, marginBottom: 8 }}>
        <div style={{ flex: 1 }}>
          <Label text="JDC" />
          <SkillButtons value={planner.jdcLevel} onChange={planner.setJdcLevel} />
        </div>
        <div style={{ flex: 1 }}>
          <Label text="JFC" />
          <SkillButtons value={planner.jfcLevel} onChange={planner.setJfcLevel} />
        </div>
      </div>

      {/* Range readout */}
      <div style={{ fontSize: 9, color: MUTED, marginBottom: 10 }}>
        RANGE: <span style={{ color: JUMP_COLOR }}>{planner.range.toFixed(1)} LY</span>
        <span style={{ marginLeft: 8 }}>
          REACHABLE: <span style={{ color: TEXT }}>{planner.reachableIds.size}</span>
        </span>
      </div>

      {/* Origin / Destination */}
      <div style={{ marginBottom: 6 }}>
        <Label text="ORIGIN" />
        <SystemSlot
          systemId={planner.jumpOrigin}
          systemName={systemName}
          onClear={() => planner.setJumpOrigin(null)}
          placeholder="Click system on map"
        />
      </div>
      <div style={{ marginBottom: 10 }}>
        <Label text="DESTINATION" />
        <SystemSlot
          systemId={planner.jumpDest}
          systemName={systemName}
          onClear={() => planner.setJumpDest(null)}
          placeholder="Click system on map"
        />
      </div>

      {/* Calculate button */}
      <button
        onClick={planner.calculate}
        disabled={planner.jumpOrigin === null || planner.jumpDest === null}
        style={{
          width: '100%', padding: '6px', fontSize: 10, letterSpacing: '0.12em',
          fontFamily: FONT, textTransform: 'uppercase',
          background: planner.jumpOrigin && planner.jumpDest ? 'rgba(255,136,0,0.15)' : 'transparent',
          color: planner.jumpOrigin && planner.jumpDest ? JUMP_COLOR : MUTED,
          border: `1px solid ${planner.jumpOrigin && planner.jumpDest ? JUMP_COLOR : BORDER}`,
          cursor: planner.jumpOrigin && planner.jumpDest ? 'pointer' : 'default',
          marginBottom: 8,
        }}
      >
        Calculate Route
      </button>

      {/* Error */}
      {planner.routeError && (
        <div style={{ fontSize: 9, color: '#cc3333', marginBottom: 8 }}>
          {planner.routeError}
        </div>
      )}

      {/* Route results */}
      {planner.jumpRoute && planner.jumpRoute.length > 1 && (
        <div>
          <div style={{ fontSize: 9, color: MUTED, letterSpacing: '0.1em', marginBottom: 6 }}>
            ROUTE: {planner.jumpRoute.length - 1} JUMP{planner.jumpRoute.length > 2 ? 'S' : ''}
          </div>

          {planner.jumpRoute.map((wp, i) => (
            <div key={wp.system.id} style={{
              padding: '4px 0',
              borderTop: i > 0 ? `1px solid ${BORDER}` : 'none',
              fontSize: 9,
            }}>
              <div style={{ color: i === 0 ? ACCENT : TEXT }}>
                {i + 1}. {wp.system.name}
                {wp.distanceLY > 0 && (
                  <span style={{ color: MUTED, marginLeft: 6 }}>
                    {wp.distanceLY.toFixed(2)} LY
                  </span>
                )}
              </div>
              {wp.fuelThisHop > 0 && (
                <div style={{ color: MUTED, fontSize: 8, marginTop: 1 }}>
                  FUEL {wp.fuelThisHop.toLocaleString()}
                  {wp.waitMinutes > 0 && (
                    <span style={{ marginLeft: 8, color: '#cc8833' }}>
                      WAIT {wp.waitMinutes.toFixed(1)}m
                    </span>
                  )}
                  {wp.orangeTimer > 0 && (
                    <span style={{ marginLeft: 8 }}>
                      CD {wp.orangeTimer.toFixed(1)}m
                    </span>
                  )}
                </div>
              )}
            </div>
          ))}

          {/* Totals */}
          <div style={{
            marginTop: 8, padding: '6px 0', borderTop: `1px solid ${BORDER}`,
            fontSize: 9, color: JUMP_COLOR,
          }}>
            <div>
              TOTAL FUEL: {planner.jumpRoute[planner.jumpRoute.length - 1].cumulativeFuel.toLocaleString()}
            </div>
            <div>
              TOTAL TIME: {planner.jumpRoute[planner.jumpRoute.length - 1].cumulativeMinutes.toFixed(1)} MIN
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Label({ text }: { text: string }) {
  return (
    <div style={{
      fontSize: 8, color: MUTED, letterSpacing: '0.12em',
      textTransform: 'uppercase', marginBottom: 3,
    }}>
      {text}
    </div>
  );
}

function SkillButtons({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <div style={{ display: 'flex', gap: 2 }}>
      {[1, 2, 3, 4, 5].map(level => (
        <button
          key={level}
          onClick={() => onChange(level)}
          style={{
            width: 22, height: 20, fontSize: 9, fontFamily: FONT,
            background: level === value ? 'rgba(200,169,81,0.15)' : 'transparent',
            color: level === value ? ACCENT : MUTED,
            border: `1px solid ${level === value ? ACCENT : BORDER}`,
            cursor: 'pointer',
          }}
        >
          {level}
        </button>
      ))}
    </div>
  );
}

function SystemSlot({
  systemId, systemName, onClear, placeholder,
}: {
  systemId: number | null;
  systemName: (id: number) => string;
  onClear: () => void;
  placeholder: string;
}) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '4px 6px', background: '#080808', border: `1px solid ${BORDER}`,
      fontSize: 10, minHeight: 24,
    }}>
      {systemId !== null ? (
        <>
          <span style={{ color: TEXT }}>{systemName(systemId)}</span>
          <button onClick={onClear} style={{
            background: 'none', border: 'none', color: MUTED, cursor: 'pointer',
            fontSize: 12, fontFamily: FONT, lineHeight: 1,
          }}>×</button>
        </>
      ) : (
        <span style={{ color: '#2a2a2a', fontSize: 9 }}>{placeholder}</span>
      )}
    </div>
  );
}
