import type { ReactNode } from 'react';

export interface BannerProps {
  tone?: 'accent' | 'danger' | 'ok';
  onDismiss?: () => void;
  children: ReactNode;
}

export function Banner({ tone = 'accent', onDismiss, children }: BannerProps) {
  const cls = ['b-banner', tone === 'danger' ? 'is-danger' : '', tone === 'ok' ? 'is-ok' : ''].filter(Boolean).join(' ');
  return (
    <div className={cls}>
      <span>{children}</span>
      {onDismiss ? (
        <button type="button" className="b-modal-close" onClick={onDismiss} aria-label="Dismiss">×</button>
      ) : null}
    </div>
  );
}
