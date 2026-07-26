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
        <h2 className="b-label" style={{ margin: 0 }}>{title}</h2>
        {actions ? <div>{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}
