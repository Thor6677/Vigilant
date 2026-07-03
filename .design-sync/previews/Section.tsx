import { Section, Panel, Table, TableRow, Badge, Button } from '@vigilant/ui';

export const WithActions = () => (
  <Section title="Recent Kills" actions={<Button variant="ghost">Refresh</Button>}>
    <Table stagger>
      <TableRow><span>Loki — J121406</span><Badge tone="ok">+412M</Badge></TableRow>
      <TableRow><span>Drake — Jita</span><Badge tone="danger">−86M</Badge></TableRow>
      <TableRow><span>Ishtar — Tama</span><Badge tone="ok">+204M</Badge></TableRow>
    </Table>
  </Section>
);

export const NoActions = () => (
  <Section title="Fleet Status">
    <Panel glass brackets>
      <div className="b-pad-md">18 pilots on grid at J121406 · Reinforced fleet doctrine</div>
    </Panel>
  </Section>
);
