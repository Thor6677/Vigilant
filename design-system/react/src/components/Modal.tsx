import { useEffect, type ReactNode } from 'react';

export interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ open, title, onClose, children }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="b-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="b-modal" role="dialog" aria-label={title}>
        <div className="b-modal-head">
          <span className="b-label">{title}</span>
          <button type="button" className="b-modal-close" onClick={onClose} aria-label="Dismiss">×</button>
        </div>
        <div className="b-modal-body">{children}</div>
      </div>
    </div>
  );
}
