import { StatBlock, StatStrip } from '@vigilant/ui';

// A single StatBlock rendered on its own has no border or background of its
// own — StatStrip (see StatStrip.tsx) is the usual parent that supplies the
// flex row, divider borders, and glass background. This card intentionally
// shows that bare, unframed state.
export const Lone = () => <StatBlock label="Fleet" value="ONLINE" tone="ok" />;

export const SingleInStrip = () => (
  <StatStrip>
    <StatBlock label="Structures" value="12" />
  </StatStrip>
);

export const ToneSweep = () => (
  <StatStrip>
    <StatBlock label="Wallet" value="4.2B ISK" />
    <StatBlock label="Skill Queue" value="3D 14H" tone="accent" />
    <StatBlock label="Alerts" value="2" tone="danger" />
    <StatBlock label="Fleet" value="ONLINE" tone="ok" />
  </StatStrip>
);
