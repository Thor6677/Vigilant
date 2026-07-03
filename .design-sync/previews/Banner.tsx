import { Banner } from '@vigilant/ui';

export const Accent = () => (
  <Banner tone="accent">ESI sync complete — 214 assets refreshed</Banner>
);

export const Danger = () => (
  <Banner tone="danger" onDismiss={() => {}}>
    Structure ALERT — Thunderborn HQ armor timer in 3h 12m
  </Banner>
);

export const Ok = () => (
  <Banner tone="ok">Fleet formed — 18 pilots on grid at J121406</Banner>
);

export const Dismissible = () => (
  <Banner tone="accent" onDismiss={() => {}}>
    New contract available — Loki hull, 480M ISK buyout
  </Banner>
);
