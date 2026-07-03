import { render, screen, fireEvent } from '@testing-library/react';
import { NavBar } from '../NavBar';
import { NavMenu } from '../NavMenu';
import { Breadcrumbs } from '../Breadcrumbs';
import { PageHeader } from '../PageHeader';
import { Section } from '../Section';
import { Panel } from '../Panel';
import { Grid } from '../Grid';
import { TabStrip } from '../TabStrip';
import { Footer } from '../Footer';

test('NavBar renders logo and children', () => {
  const { container } = render(
    <NavBar logo="VIGILANT" logoHref="/">
      <a className="b-nav-link" href="/intel">Intel</a>
    </NavBar>
  );
  expect(container.querySelector('.b-nav')).toBeTruthy();
  expect(screen.getByText('VIGILANT').className).toContain('b-nav-logo');
});

test('NavMenu renders dropdown items', () => {
  const { container } = render(
    <NavMenu label="Intel" items={[
      { label: 'Kill Feed', href: '/intel/kills', active: true },
      { label: 'D-Scan', href: '/intel/dscan' },
    ]} />
  );
  expect(container.querySelector('.b-nav-dropdown-menu')).toBeTruthy();
  expect(screen.getByText('Kill Feed').className).toContain('is-active');
});

test('Breadcrumbs renders crumbs with separators and current', () => {
  const { container } = render(
    <Breadcrumbs crumbs={[{ label: 'Intel', href: '/intel' }, { label: 'Kills' }]} />
  );
  expect(container.querySelectorAll('.b-crumb-sep')).toHaveLength(1);
  expect(screen.getByText('Kills').className).toContain('b-crumb-current');
});

test('PageHeader renders title and actions', () => {
  render(<PageHeader title="Dashboard" actions={<button>Refresh</button>} />);
  expect(screen.getByText('Dashboard').className).toContain('b-page-title');
  expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
});

test('Section renders head label and children', () => {
  render(<Section title="Recent Kills"><p>rows</p></Section>);
  expect(screen.getByText('Recent Kills').className).toContain('b-label');
  expect(screen.getByText('rows')).toBeTruthy();
});

test('Panel glass + brackets modifiers', () => {
  const { container } = render(<Panel title="Fleet" glass brackets>body</Panel>);
  const el = container.querySelector('.b-panel')!;
  expect(el.className).toContain('is-glass');
  expect(el.className).toContain('is-brackets');
});

test('Grid cols 2 and 3', () => {
  const g2 = render(<Grid cols={2}>x</Grid>).container.firstElementChild!;
  const g3 = render(<Grid cols={3}>x</Grid>).container.firstElementChild!;
  expect(g2.className).toContain('b-grid-2');
  expect(g3.className).toContain('b-grid-3');
});

test('TabStrip active tab + onSelect', () => {
  const onSelect = vi.fn();
  render(<TabStrip tabs={[{ label: 'Alpha', active: true }, { label: 'Beta' }]} onSelect={onSelect} />);
  expect(screen.getByText('Alpha').className).toContain('is-active');
  screen.getByText('Beta').click();
  expect(onSelect).toHaveBeenCalledWith(1);
});

test('Footer renders links and brand', () => {
  render(<Footer links={[{ label: 'GitHub', href: 'https://github.com' }]} brand="THUNDERBORN" />);
  expect(screen.getByText('GitHub').className).toContain('b-footer-link');
  expect(screen.getByText('THUNDERBORN')).toBeTruthy();
});

test('TabStrip link tabs render anchors and do not fire onSelect', () => {
  const onSelect = vi.fn();
  render(<TabStrip tabs={[{ label: 'Docs', href: '/docs' }]} onSelect={onSelect} />);
  const link = screen.getByText('Docs');
  expect(link.tagName).toBe('A');
  fireEvent.click(link);
  expect(onSelect).not.toHaveBeenCalled();
});

test('Panel without title renders no head', () => {
  const { container } = render(<Panel>content</Panel>);
  expect(container.querySelector('.b-panel-head')).toBeNull();
});

test('NavBar renders right slot', () => {
  render(<NavBar logo="V" right={<span>acct</span>} />);
  expect(screen.getByText('acct')).toBeTruthy();
});

test('Breadcrumbs is a labeled nav with aria-current on the last crumb', () => {
  const { container } = render(<Breadcrumbs crumbs={[{ label: 'Home', href: '/' }, { label: 'Here' }]} />);
  const nav = container.querySelector('nav.b-breadcrumbs');
  expect(nav?.getAttribute('aria-label')).toBe('Breadcrumbs');
  expect(screen.getByText('Here').getAttribute('aria-current')).toBe('page');
});

test('NavMenu honors explicit trigger href and hides caret from AT', () => {
  const { container } = render(<NavMenu label="Intel" href="/intel" items={[{ label: 'Kills', href: '/intel/kills' }]} />);
  const trigger = container.querySelector('a.b-nav-link')!;
  expect(trigger.getAttribute('href')).toBe('/intel');
  expect(trigger.getAttribute('aria-haspopup')).toBe('true');
  expect(container.querySelector('[aria-hidden="true"]')).toBeTruthy();
});
