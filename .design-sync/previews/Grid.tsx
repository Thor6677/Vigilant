import { Grid, Panel, KeyValueRow, ProgressBar, EmptyState, Skeleton } from '@vigilant/ui';

export const TwoCols = () => (
  <Grid cols={2}>
    <Panel title="Fleet Status" glass brackets>
      <KeyValueRow label="Thunderborn HQ" value="ONLINE" tone="ok" />
      <KeyValueRow label="Fuel" value="42 days" tone="warn" />
      <KeyValueRow label="Reinforced" value="—" tone="muted" />
      <div className="b-pad-md"><ProgressBar value={72} tone="warn" /></div>
    </Panel>
    <Panel title="Loading States">
      <div className="b-pad-md"><Skeleton lines={3} /></div>
      <EmptyState>No contracts found</EmptyState>
    </Panel>
  </Grid>
);

export const ThreeCols = () => (
  <Grid cols={3}>
    <Panel title="Jita">
      <div className="b-pad-md">1,204 orders · 42.1B ISK volume</div>
    </Panel>
    <Panel title="Amarr">
      <div className="b-pad-md">318 orders · 6.7B ISK volume</div>
    </Panel>
    <Panel title="Dodixie">
      <div className="b-pad-md">96 orders · 1.2B ISK volume</div>
    </Panel>
  </Grid>
);
