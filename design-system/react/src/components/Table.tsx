import type { ReactNode } from 'react';

export interface TableProps {
  /** optional b-panel-head title */
  title?: string;
  /** staggered row entrance animation */
  stagger?: boolean;
  children: ReactNode;
}

export function Table({ title, stagger = false, children }: TableProps) {
  return (
    <div className="b-panel">
      {title ? (
        <div className="b-panel-head"><span className="b-label">{title}</span></div>
      ) : null}
      <div className={stagger ? 'vg-stagger' : undefined}>{children}</div>
    </div>
  );
}

export interface TableRowProps {
  children: ReactNode;
  onClick?: () => void;
}

export function TableRow({ children, onClick }: TableRowProps) {
  return (
    <div
      className="b-table-row"
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } } : undefined}
    >
      {children}
    </div>
  );
}
