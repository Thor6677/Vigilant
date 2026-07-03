export interface ProgressBarProps {
  /** 0–100; values outside the range are clamped */
  value: number;
  tone?: 'default' | 'active' | 'warn' | 'danger';
}

export function ProgressBar({ value, tone = 'default' }: ProgressBarProps) {
  const pct = Math.max(0, Math.min(100, value));
  const cls = ['b-progress-fill', tone === 'active' ? 'is-active' : '', tone === 'warn' ? 'is-warn' : '', tone === 'danger' ? 'is-crit' : ''].filter(Boolean).join(' ');
  return (
    <div className="b-progress">
      <div className={cls} style={{ width: `${pct}%` }} />
    </div>
  );
}
