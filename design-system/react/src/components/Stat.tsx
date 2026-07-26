import type { ReactNode } from 'react';
import { toneClass, type Tone } from './tones';

export interface StatStripProps {
  children: ReactNode;
}

export function StatStrip({ children }: StatStripProps) {
  return <div className="b-stats">{children}</div>;
}

export interface StatBlockProps {
  label: string;
  value: ReactNode;
  tone?: Tone;
}

export function StatBlock({ label, value, tone }: StatBlockProps) {
  return (
    <div className="b-stat">
      <div className={`b-stat-val ${toneClass(tone)}`.trim()}>{value}</div>
      <div className="b-stat-label">{label}</div>
    </div>
  );
}
