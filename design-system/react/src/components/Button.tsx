import type { ButtonHTMLAttributes, ReactNode } from 'react';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 'strip' = card action-strip button (default); 'primary' = gold solid with shine; 'ghost' = glass outline */
  variant?: 'strip' | 'primary' | 'ghost';
  /** danger tone (red hover/border) */
  danger?: boolean;
  children: ReactNode;
}

export function Button({ variant = 'strip', danger = false, className = '', children, ...rest }: ButtonProps) {
  const cls = [
    'b-btn',
    variant === 'primary' ? 'is-primary' : '',
    variant === 'ghost' ? 'is-ghost' : '',
    danger ? 'is-danger' : '',
    className,
  ].filter(Boolean).join(' ');
  return (
    <button className={cls} {...rest}>
      {children}
    </button>
  );
}
