import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

export interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ open, title, onClose, children }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    // Multiple open modals all close on one Escape — acceptable for prototype rendering.
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    dialogRef.current?.focus();
  }, [open]);

  if (!open) return null;
  // Portaled: backdrop-filter ancestors (glass panels) become containing blocks for position:fixed, which would trap the overlay inside the panel.
  return createPortal(
    <div className="b-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={dialogRef} tabIndex={-1} className="b-modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="b-modal-head">
          <span className="b-label">{title}</span>
          <button type="button" className="b-modal-close" onClick={onClose} aria-label="Dismiss">×</button>
        </div>
        <div className="b-modal-body">{children}</div>
      </div>
    </div>,
    document.body
  );
}
