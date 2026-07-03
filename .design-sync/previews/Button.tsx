import { Button } from '@vigilant/ui';

export const Primary = () => <Button variant="primary">Scan System</Button>;

export const Ghost = () => <Button variant="ghost">Refresh Assets</Button>;

export const GhostDanger = () => (
  <Button variant="ghost" danger>
    Abandon Structure
  </Button>
);

export const SideBySide = () => (
  <div style={{ display: 'flex', gap: '8px' }}>
    <Button variant="primary">Jump</Button>
    <Button variant="ghost">Cancel</Button>
  </div>
);
