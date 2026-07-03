import { NavBar, NavMenu } from '@vigilant/ui';

export const Basic = () => (
  <NavBar logo="VIGILANT">
    <a className="b-nav-link" href="#kills">Intel</a>
    <a className="b-nav-link" href="#jobs">Industry</a>
    <a className="b-nav-link" href="#map">Map</a>
  </NavBar>
);

export const WithNavMenuAndRight = () => (
  <NavBar
    logo="VIGILANT"
    right={<>
      <a className="b-nav-link" href="#account">THUNDERBORN HQ</a>
      <a className="b-nav-link" href="#logout">LOGOUT</a>
    </>}
  >
    <NavMenu label="Intel" active items={[
      { label: 'Kill Feed', href: '#kills', active: true },
      { label: 'D-Scan', href: '#dscan' },
      { label: 'Local Watch', href: '#local' },
    ]} />
    <NavMenu label="Industry" items={[
      { label: 'Jobs', href: '#jobs' },
      { label: 'Blueprints', href: '#bp' },
    ]} />
    <a className="b-nav-link" href="#assets">Assets</a>
    <a className="b-nav-link" href="#map">Map</a>
  </NavBar>
);

export const CustomLogoHref = () => (
  <NavBar logo="VIGILANT" logoHref="#dashboard">
    <a className="b-nav-link" href="#assets">Assets</a>
    <a className="b-nav-link" href="#contracts">Contracts</a>
  </NavBar>
);
