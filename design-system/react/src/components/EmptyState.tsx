import type { ReactNode } from 'react';

export interface EmptyStateProps {
  children: ReactNode;
}

export function EmptyState({ children }: EmptyStateProps) {
  return <div className="b-empty">{children}</div>;
}
