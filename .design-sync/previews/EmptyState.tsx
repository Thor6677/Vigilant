import { EmptyState, Panel } from '@vigilant/ui';

export const NoContracts = () => (
  <Panel title="Contracts">
    <EmptyState>No contracts found</EmptyState>
  </Panel>
);

export const NoKills = () => (
  <Panel title="Kill Feed">
    <EmptyState>No kills recorded in the last 24 hours</EmptyState>
  </Panel>
);
