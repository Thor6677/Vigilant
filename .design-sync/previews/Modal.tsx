import { Modal, KeyValueRow, Button } from '@vigilant/ui';

export const ConfirmJump = () => (
  <Modal open title="Confirm Jump" onClose={() => {}}>
    <KeyValueRow label="Destination" value="J121406" />
    <KeyValueRow label="Topology" value="C5 → C3 → LS" tone="accent" />
    <KeyValueRow label="Mass Status" value="Reduced (2 jumps)" tone="warn" />
    <div style={{ marginTop: '1rem', display: 'flex', gap: '8px' }}>
      <Button variant="primary" onClick={() => {}}>Jump</Button>
      <Button variant="ghost" onClick={() => {}}>Cancel</Button>
    </div>
  </Modal>
);
