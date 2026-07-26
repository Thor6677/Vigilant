import { Fragment } from 'react';

export interface Crumb {
  label: string;
  href?: string;
}

export interface BreadcrumbsProps {
  crumbs: Crumb[];
}

export function Breadcrumbs({ crumbs }: BreadcrumbsProps) {
  return (
    <nav aria-label="Breadcrumbs" className="b-breadcrumbs">
      {crumbs.map((c, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="b-crumb-sep">/</span>}
          {c.href ? <a href={c.href}>{c.label}</a> : <span className="b-crumb-current" aria-current="page">{c.label}</span>}
        </Fragment>
      ))}
    </nav>
  );
}
