import { Breadcrumbs } from '@vigilant/ui';

export const MultiCrumb = () => (
  <Breadcrumbs crumbs={[
    { label: 'Home', href: '#' },
    { label: 'Intel', href: '#intel' },
    { label: 'Kill Feed' },
  ]} />
);

export const DeepPath = () => (
  <Breadcrumbs crumbs={[
    { label: 'Home', href: '#' },
    { label: 'Wormholes', href: '#wh' },
    { label: 'J121406', href: '#wh/j121406' },
    { label: 'Structure Timers' },
  ]} />
);

export const SingleCrumb = () => (
  <Breadcrumbs crumbs={[{ label: 'Dashboard' }]} />
);
