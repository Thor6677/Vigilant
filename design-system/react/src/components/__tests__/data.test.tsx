import { render, screen, fireEvent } from '@testing-library/react';
import { StatStrip, StatBlock } from '../Stat';
import { KeyValueRow } from '../KeyValueRow';
import { Table, TableRow } from '../Table';
import { Badge } from '../Badge';
import { ProgressBar } from '../ProgressBar';
import { EmptyState } from '../EmptyState';
import { Eyebrow } from '../Eyebrow';

test('StatStrip + StatBlock render values with tones', () => {
  const { container } = render(
    <StatStrip>
      <StatBlock label="Wallet" value="4.2B ISK" />
      <StatBlock label="Alerts" value="2" tone="danger" />
    </StatStrip>
  );
  expect(container.querySelector('.b-stats')).toBeTruthy();
  expect(screen.getByText('2').className).toContain('is-danger');
  expect(screen.getByText('Wallet')).toBeTruthy();
});

test('KeyValueRow renders label/value with tone', () => {
  render(<KeyValueRow label="Fuel" value="42 days" tone="warn" />);
  expect(screen.getByText('Fuel').className).toContain('b-row-label');
  expect(screen.getByText('42 days').className).toContain('is-warn');
});

test('Table renders rows in a panel with stagger', () => {
  const { container } = render(
    <Table stagger>
      <TableRow><span>Loki</span><span>+412M</span></TableRow>
      <TableRow><span>Drake</span><span>−86M</span></TableRow>
    </Table>
  );
  expect(container.querySelector('.b-panel')).toBeTruthy();
  expect(container.querySelector('.vg-stagger')).toBeTruthy();
  expect(container.querySelectorAll('.b-table-row')).toHaveLength(2);
});

test('Badge tones and active', () => {
  render(<Badge tone="danger">HOSTILE</Badge>);
  expect(screen.getByText('HOSTILE').className).toContain('is-danger');
  render(<Badge active>ONLINE</Badge>);
  expect(screen.getByText('ONLINE').className).toContain('is-active');
});

test('ProgressBar clamps and maps tone', () => {
  const { container } = render(<ProgressBar value={150} tone="danger" />);
  const fill = container.querySelector('.b-progress-fill') as HTMLElement;
  expect(fill.style.width).toBe('100%');
  expect(fill.className).toContain('is-crit');
});

test('EmptyState and Eyebrow', () => {
  render(<EmptyState>No kills recorded</EmptyState>);
  expect(screen.getByText('No kills recorded').className).toContain('b-empty');
  render(<Eyebrow>Intel</Eyebrow>);
  expect(screen.getByText('Intel').className).toContain('b-eyebrow');
});

test('clickable TableRow is keyboard-activatable', () => {
  const onClick = vi.fn();
  render(<Table><TableRow onClick={onClick}><span>Row</span></TableRow></Table>);
  const row = screen.getByRole('button');
  expect(row.getAttribute('tabindex')).toBe('0');
  fireEvent.keyDown(row, { key: 'Enter' });
  fireEvent.keyDown(row, { key: ' ' });
  fireEvent.click(row);
  expect(onClick).toHaveBeenCalledTimes(3);
});

test('Table title renders panel head', () => {
  const { container } = render(<Table title="Kills"><TableRow><span>x</span></TableRow></Table>);
  expect(container.querySelector('.b-panel-head')).toBeTruthy();
});

test('ProgressBar clamps low, handles NaN, exposes aria', () => {
  const { container, rerender } = render(<ProgressBar value={-5} />);
  const bar = container.querySelector('.b-progress') as HTMLElement;
  const fill = container.querySelector('.b-progress-fill') as HTMLElement;
  expect(fill.style.width).toBe('0%');
  expect(fill.className.trim()).toBe('b-progress-fill');
  expect(bar.getAttribute('role')).toBe('progressbar');
  rerender(<ProgressBar value={NaN} />);
  expect((container.querySelector('.b-progress-fill') as HTMLElement).style.width).toBe('0%');
});

test('Badge active takes precedence over tone', () => {
  render(<Badge active tone="danger">FLAG</Badge>);
  const el = screen.getByText('FLAG');
  expect(el.className).toContain('is-active');
  expect(el.className).not.toContain('is-danger');
});

test('StatBlock without tone has clean className', () => {
  render(<StatBlock label="Fuel" value="42d" />);
  const val = screen.getByText('42d');
  expect(val.className).toBe('b-stat-val');
});
