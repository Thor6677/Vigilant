import type { ReactNode } from 'react';

export interface ToastProps {
  tone?: 'accent' | 'ok' | 'danger' | 'info';
  children: ReactNode;
}

export function Toast({ tone = 'accent', children }: ToastProps) {
  const cls = ['b-toast', tone === 'ok' ? 'is-ok' : '', tone === 'danger' ? 'is-danger' : '', tone === 'info' ? 'is-info' : ''].filter(Boolean).join(' ');
  return <div className={cls}>{children}</div>;
}

export interface ToastStackProps {
  children: ReactNode;
}

export function ToastStack({ children }: ToastStackProps) {
  return <div className="b-toast-stack">{children}</div>;
}
