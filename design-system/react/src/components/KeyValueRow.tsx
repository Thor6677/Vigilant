import type { ReactNode } from 'react';
import { toneClass, type Tone } from './tones';

export interface KeyValueRowProps {
  label: string;
  value: ReactNode;
  tone?: Tone;
}

export function KeyValueRow({ label, value, tone }: KeyValueRowProps) {
  return (
    <div className="b-row">
      <span className="b-row-label">{label}</span>
      <span className={`b-row-val ${toneClass(tone)}`.trim()}>{value}</span>
    </div>
  );
}
