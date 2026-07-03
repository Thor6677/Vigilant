import { TabStrip } from '@vigilant/ui';

export const ButtonTabs = () => (
  <TabStrip
    tabs={[
      { label: 'Overview', active: true },
      { label: 'Assets' },
      { label: 'Journal' },
    ]}
    onSelect={() => {}}
  />
);

export const LinkTabs = () => (
  <TabStrip
    tabs={[
      { label: 'Kill Feed', href: '#kills', active: true },
      { label: 'D-Scan', href: '#dscan' },
      { label: 'Local Watch', href: '#local' },
    ]}
  />
);

export const ManyTabs = () => (
  <TabStrip
    tabs={[
      { label: 'Summary' },
      { label: 'Fittings', active: true },
      { label: 'Blueprints' },
      { label: 'Contracts' },
      { label: 'Wallet' },
    ]}
    onSelect={() => {}}
  />
);
