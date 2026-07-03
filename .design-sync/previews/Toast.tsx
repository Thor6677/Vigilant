import { Toast } from '@vigilant/ui';

export const Ok = () => <Toast tone="ok">Fit saved</Toast>;

export const Danger = () => (
  <Toast tone="danger" onDismiss={() => {}}>
    ESI token expired — reauthenticate
  </Toast>
);

export const Info = () => <Toast tone="info">ESI sync complete</Toast>;

export const Accent = () => <Toast tone="accent">Skill queue updated</Toast>;

export const Stacked = () => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
    <Toast tone="ok">Contract accepted — Drake, 86M ISK</Toast>
    <Toast tone="info">D-Scan refreshed — 12 signatures</Toast>
    <Toast tone="danger" onDismiss={() => {}}>Jump clone timer expired</Toast>
  </div>
);
