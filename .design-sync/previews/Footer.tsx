import { Footer } from '@vigilant/ui';

export const WithBrand = () => (
  <Footer
    links={[
      { label: 'GitHub', href: '#' },
      { label: 'Status', href: '#status' },
      { label: 'Support', href: '#support' },
    ]}
    brand="THUNDERBORN"
  />
);

export const LinksOnly = () => (
  <Footer links={[{ label: 'Privacy', href: '#privacy' }, { label: 'Terms', href: '#terms' }]} />
);

export const BrandOnly = () => <Footer brand="THUNDERBORN" />;
