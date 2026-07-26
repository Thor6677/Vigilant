import type { ReactNode } from 'react';

export interface GridProps {
  cols: 2 | 3;
  children: ReactNode;
}

export function Grid({ cols, children }: GridProps) {
  return <div className={cols === 3 ? 'b-grid-3' : 'b-grid-2'}>{children}</div>;
}
