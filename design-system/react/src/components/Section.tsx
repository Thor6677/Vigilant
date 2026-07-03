import type { ReactNode } from 'react';

export interface SectionProps {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function Section({ title, actions, children }: SectionProps) {
  return (
    <section className="b-section">
      <div className="b-section-head">
        <span className="b-label">{title}</span>
        {actions ? <div>{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}
