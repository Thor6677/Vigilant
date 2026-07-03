import { Panel, KeyValueRow, ButtonGroup, Button } from '@vigilant/ui';

export const GlassWithBrackets = () => (
  <Panel title="Fleet Status" glass brackets>
    <KeyValueRow label="Thunderborn HQ" value="ONLINE" tone="ok" />
    <KeyValueRow label="Fuel" value="42 days" tone="warn" />
    <KeyValueRow label="Reinforced" value="—" tone="muted" />
  </Panel>
);

export const PlainWithActions = () => (
  <Panel title="Corp Hangar">
    <div className="b-pad-md">3,214 items · est. 4.2B ISK</div>
    <ButtonGroup>
      <Button>View</Button>
      <Button>Appraise</Button>
      <Button danger>Trash</Button>
    </ButtonGroup>
  </Panel>
);

export const Untitled = () => (
  <Panel>
    <div className="b-pad-md">
      Wormhole J121406 — class C5, Pulsar effect. Static to C3 and lowsec.
    </div>
  </Panel>
);
