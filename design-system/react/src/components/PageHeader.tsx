import type { ReactNode } from 'react';

export interface PageHeaderProps {
  title: string;
  actions?: ReactNode;
}

export function PageHeader({ title, actions }: PageHeaderProps) {
  return (
    <div className="b-page-header">
      <h1 className="b-page-title">{title}</h1>
      {actions ? <div>{actions}</div> : null}
    </div>
  );
}
