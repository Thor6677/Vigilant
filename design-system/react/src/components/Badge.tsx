import type { ReactNode } from 'react';
import { toneClass, type Tone } from './tones';

export interface BadgeProps {
  tone?: Tone;
  /** inverted (filled) style */
  active?: boolean;
  children: ReactNode;
}

export function Badge({ tone, active = false, children }: BadgeProps) {
  const cls = ['b-badge', active ? 'is-active' : toneClass(tone)].filter(Boolean).join(' ');
  return <span className={cls}>{children}</span>;
}
