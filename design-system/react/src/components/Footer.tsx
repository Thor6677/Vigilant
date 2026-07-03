export interface FooterLink {
  label: string;
  href: string;
}

export interface FooterProps {
  links?: FooterLink[];
  brand?: string;
}

export function Footer({ links = [], brand }: FooterProps) {
  return (
    <footer className="b-footer">
      <div className="b-footer-links">
        {links.map((l) => (
          <a key={l.href} className="b-footer-link" href={l.href}>{l.label}</a>
        ))}
      </div>
      {brand ? <span className="b-footer-brand">{brand}</span> : null}
    </footer>
  );
}
