import type { ReactNode } from 'react';

export interface NavBarProps {
  logo: string;
  logoHref?: string;
  /** nav links / NavMenu elements */
  children?: ReactNode;
  /** right-aligned content (auth state, server bar, etc.) */
  right?: ReactNode;
}

export function NavBar({ logo, logoHref = '/', children, right }: NavBarProps) {
  return (
    <nav className="b-nav">
      <a className="b-nav-logo" href={logoHref}>{logo}</a>
      <div className="b-nav-links">{children}</div>
      {right ? <div className="b-nav-links">{right}</div> : null}
    </nav>
  );
}
