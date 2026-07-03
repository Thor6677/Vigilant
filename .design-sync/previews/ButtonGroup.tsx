import { ButtonGroup, Button, Panel } from '@vigilant/ui';

export const ViewAppraiseTrash = () => (
  <Panel title="Corp Hangar">
    <div className="b-pad-md">3,214 items · est. 4.2B ISK</div>
    <ButtonGroup>
      <Button>View</Button>
      <Button>Appraise</Button>
      <Button danger>Trash</Button>
    </ButtonGroup>
  </Panel>
);

export const TwoActions = () => (
  <Panel title="Contract — Loki Hull">
    <div className="b-pad-md">480M ISK buyout · expires in 2 days</div>
    <ButtonGroup>
      <Button variant="primary">Accept</Button>
      <Button variant="ghost">Decline</Button>
    </ButtonGroup>
  </Panel>
);
