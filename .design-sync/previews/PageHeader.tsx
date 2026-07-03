import { PageHeader, Button, ButtonGroup } from '@vigilant/ui';

export const TitleOnly = () => <PageHeader title="Structure Timers" />;

export const WithPrimaryAction = () => (
  <PageHeader
    title="Kill Feed"
    actions={<Button variant="primary">New Alert Filter</Button>}
  />
);

export const WithButtonGroup = () => (
  <PageHeader
    title="Contracts"
    actions={
      <ButtonGroup>
        <Button variant="ghost">Refresh</Button>
        <Button variant="primary">Create Contract</Button>
      </ButtonGroup>
    }
  />
);
