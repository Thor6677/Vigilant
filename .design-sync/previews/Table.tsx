import { Table, TableRow, Badge } from '@vigilant/ui';

export const RecentKills = () => (
  <Table title="Recent Kills" stagger>
    <TableRow>
      <span>Loki — J121406</span>
      <Badge tone="ok">+412M</Badge>
    </TableRow>
    <TableRow>
      <span>Drake — Jita</span>
      <Badge tone="danger">−86M</Badge>
    </TableRow>
    <TableRow>
      <span>Ishtar — Tama</span>
      <Badge tone="ok">+204M</Badge>
    </TableRow>
  </Table>
);

export const Untitled = () => (
  <Table>
    <TableRow>
      <span>Keepstar — J121406</span>
      <Badge tone="ok">92% fuel</Badge>
    </TableRow>
    <TableRow>
      <span>Astrahus — Tama</span>
      <Badge tone="warn">14% fuel</Badge>
    </TableRow>
  </Table>
);
