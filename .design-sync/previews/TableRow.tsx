import { Table, TableRow, Badge } from '@vigilant/ui';

export const Clickable = () => (
  <Table title="Structures">
    <TableRow onClick={() => {}}>
      <span>Keepstar — J121406</span>
      <Badge tone="ok">92% fuel</Badge>
    </TableRow>
  </Table>
);

export const Plain = () => (
  <Table title="Structures">
    <TableRow>
      <span>Astrahus — Tama</span>
      <Badge tone="warn">14% fuel</Badge>
    </TableRow>
  </Table>
);

export const SideBySide = () => (
  <Table title="Structures">
    <TableRow onClick={() => {}}>
      <span>Keepstar — J121406 (click for detail)</span>
      <Badge tone="ok">92% fuel</Badge>
    </TableRow>
    <TableRow>
      <span>Astrahus — Tama (static row)</span>
      <Badge tone="warn">14% fuel</Badge>
    </TableRow>
  </Table>
);
