import { Panel, KeyValueRow, ProgressBar } from '@vigilant/ui';

export const FuelSweep = () => (
  <Panel title="Structure Fuel" glass brackets>
    <KeyValueRow label="Keepstar — J121406" value="72%" />
    <div className="b-pad-md"><ProgressBar value={72} /></div>
    <KeyValueRow label="Astrahus — Tama" value="41%" tone="warn" />
    <div className="b-pad-md"><ProgressBar value={41} tone="warn" /></div>
    <KeyValueRow label="Raitaru — Jita" value="8%" tone="danger" />
    <div className="b-pad-md"><ProgressBar value={8} tone="danger" /></div>
  </Panel>
);

export const SkillQueueActive = () => (
  <Panel title="Skill Queue">
    <KeyValueRow label="Training: Battleship V" value="63%" tone="accent" />
    <div className="b-pad-md"><ProgressBar value={63} tone="active" /></div>
  </Panel>
);
