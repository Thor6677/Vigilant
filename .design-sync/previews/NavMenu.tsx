import { NavMenu, NavBar } from '@vigilant/ui';

export const Basic = () => (
  <NavMenu label="Intel" items={[
    { label: 'Kill Feed', href: '#kills' },
    { label: 'D-Scan', href: '#dscan' },
    { label: 'Local Watch', href: '#local' },
  ]} />
);

export const ActiveTrigger = () => (
  <NavMenu label="Industry" active items={[
    { label: 'Jobs', href: '#jobs', active: true },
    { label: 'Blueprints', href: '#bp' },
    { label: 'Reactions', href: '#reactions' },
  ]} />
);

export const InsideNavBar = () => (
  <NavBar logo="VIGILANT" right={<a className="b-nav-link" href="#logout">LOGOUT</a>}>
    <NavMenu label="Intel" active items={[
      { label: 'Kill Feed', href: '#kills', active: true },
      { label: 'D-Scan', href: '#dscan' },
      { label: 'Local Watch', href: '#local' },
    ]} />
    <NavMenu label="Industry" items={[
      { label: 'Jobs', href: '#jobs' },
      { label: 'Blueprints', href: '#bp' },
    ]} />
    <a className="b-nav-link" href="#map">Map</a>
  </NavBar>
);
