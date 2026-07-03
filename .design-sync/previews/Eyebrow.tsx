import { Eyebrow, Panel } from '@vigilant/ui';

export const AboveHeading = () => (
  <div>
    <Eyebrow>Structure Alert</Eyebrow>
    <h2 style={{ margin: '4px 0 0', color: 'var(--text)' }}>Thunderborn HQ</h2>
  </div>
);

export const InPanel = () => (
  <Panel title="Hangar">
    <div className="b-pad-md">
      <Eyebrow>Card actions</Eyebrow>
      <div style={{ marginTop: '8px' }}>3,214 items · est. 4.2B ISK</div>
    </div>
  </Panel>
);
