import { Skeleton, Panel } from '@vigilant/ui';

export const Default = () => (
  <Panel title="Corp Hangar">
    <div className="b-pad-md">
      <Skeleton />
    </div>
  </Panel>
);

export const CustomWidth = () => (
  <Panel title="Killmail Detail">
    <div className="b-pad-md">
      <Skeleton lines={5} lastLineWidth="30%" />
    </div>
  </Panel>
);
