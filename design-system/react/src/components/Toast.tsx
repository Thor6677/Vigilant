import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';

export interface ToastProps {
  tone?: 'accent' | 'ok' | 'danger' | 'info';
  onDismiss?: () => void;
  children: ReactNode;
}

export function Toast({ tone = 'accent', onDismiss, children }: ToastProps) {
  // 'accent' is the base styling (no class) — deliberately not toneClass(), whose is-accent has no CSS here.
  const cls = ['b-toast', tone === 'ok' ? 'is-ok' : '', tone === 'danger' ? 'is-danger' : '', tone === 'info' ? 'is-info' : ''].filter(Boolean).join(' ');
  return (
    <div className={cls} role="status">
      {children}
      {onDismiss ? (
        <button type="button" className="b-modal-close" onClick={onDismiss} aria-label="Dismiss">×</button>
      ) : null}
    </div>
  );
}

export interface ToastStackProps {
  children: ReactNode;
}

export function ToastStack({ children }: ToastStackProps) {
  // Portaled: backdrop-filter ancestors (glass panels) become containing blocks for position:fixed, which would trap the overlay inside the panel.
  return createPortal(<div className="b-toast-stack">{children}</div>, document.body);
}
