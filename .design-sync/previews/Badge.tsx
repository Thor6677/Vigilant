import { Badge } from '@vigilant/ui';

export const Tones = () => (
  <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
    <Badge>NEUTRAL</Badge>
    <Badge tone="ok">+412M</Badge>
    <Badge tone="warn">LOWSEC</Badge>
    <Badge tone="danger">HOSTILE</Badge>
  </div>
);

export const ActiveInverted = () => <Badge active>ONLINE</Badge>;

export const InContext = () => (
  <span style={{ display: 'inline-flex', gap: '8px', alignItems: 'center' }}>
    <span className="b-text">Loki — J121406</span>
    <Badge tone="ok">+412M</Badge>
  </span>
);
