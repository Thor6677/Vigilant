import type { ReactNode } from 'react';

export interface PanelProps {
  title?: string;
  actions?: ReactNode;
  /** translucent glass surface with blur */
  glass?: boolean;
  /** gold corner brackets */
  brackets?: boolean;
  children: ReactNode;
}

export function Panel({ title, actions, glass = false, brackets = false, children }: PanelProps) {
  const cls = ['b-panel', glass ? 'is-glass' : '', brackets ? 'is-brackets' : ''].filter(Boolean).join(' ');
  return (
    <div className={cls}>
      {title ? (
        <div className="b-panel-head">
          <span className="b-label">{title}</span>
          {actions ? <div>{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}
