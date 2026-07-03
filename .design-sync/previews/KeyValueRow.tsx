import { Panel, KeyValueRow } from '@vigilant/ui';

export const FleetStatus = () => (
  <Panel title="Fleet Status" glass brackets>
    <KeyValueRow label="Thunderborn HQ" value="ONLINE" tone="ok" />
    <KeyValueRow label="Fuel" value="42 days" tone="warn" />
    <KeyValueRow label="Hostiles" value="3 in local" tone="danger" />
    <KeyValueRow label="Reinforced" value="—" tone="muted" />
  </Panel>
);

export const CorpFinance = () => (
  <Panel title="Corp Wallet">
    <KeyValueRow label="Balance" value="4.2B ISK" />
    <KeyValueRow label="Daily P&L" value="+218M ISK" tone="accent" />
  </Panel>
);
