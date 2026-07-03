import { StatStrip, StatBlock } from '@vigilant/ui';

export const Dashboard = () => (
  <StatStrip>
    <StatBlock label="Wallet" value="4.2B ISK" />
    <StatBlock label="Skill Queue" value="3D 14H" tone="accent" />
    <StatBlock label="Alerts" value="2" tone="danger" />
    <StatBlock label="Fleet" value="ONLINE" tone="ok" />
  </StatStrip>
);

export const Compact = () => (
  <StatStrip>
    <StatBlock label="Fuel" value="6 days" tone="danger" />
    <StatBlock label="Reinforced" value="18:42:00" tone="accent" />
  </StatStrip>
);
