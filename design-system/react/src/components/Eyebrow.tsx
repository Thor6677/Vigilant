import type { ReactNode } from 'react';

export interface EyebrowProps {
  children: ReactNode;
}

export function Eyebrow({ children }: EyebrowProps) {
  return <span className="b-eyebrow">{children}</span>;
}
