import { useEffect, useRef, useState } from 'react';

/**
 * Mobile bottom-sheet container with a drag handle and three snap points
 * (peek / half / full). Pulled open on drag-up, dismissed on drag-down
 * past the peek threshold.
 *
 * Follows the Material/Google Maps pattern: the sheet co-exists with the map
 * below and can be panned independently. On desktop, the parent decides to
 * skip rendering this wrapper and use the regular side-panel layout.
 */
export type SheetSnap = 'peek' | 'half' | 'full';

interface Props {
  open: boolean;
  initialSnap?: SheetSnap;
  onClose?: () => void;
  title?: string;
  children: React.ReactNode;
  /** Measured pixel heights for each snap. If omitted, fallbacks are used. */
  snapPeekPx?: number;
  snapHalfPct?: number;   // 0 – 1 of window height
  snapFullPct?: number;
}

export function BottomSheet({
  open,
  initialSnap = 'half',
  onClose,
  title,
  children,
  snapPeekPx = 100,
  snapHalfPct = 0.5,
  snapFullPct = 0.92,
}: Props) {
  const [snap, setSnap] = useState<SheetSnap>(initialSnap);
  const [dragY, setDragY] = useState(0);
  const dragStartYRef = useRef<number | null>(null);
  const sheetRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) setSnap(initialSnap);
  }, [open, initialSnap]);

  if (!open) return null;

  const winH = window.innerHeight;
  const heights: Record<SheetSnap, number> = {
    peek: snapPeekPx,
    half: Math.round(winH * snapHalfPct),
    full: Math.round(winH * snapFullPct),
  };
  const height = heights[snap] + dragY;

  // Drag handlers — use pointer events so touch and mouse both work.
  const onHandleDown = (e: React.PointerEvent) => {
    dragStartYRef.current = e.clientY;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onHandleMove = (e: React.PointerEvent) => {
    if (dragStartYRef.current == null) return;
    const dy = dragStartYRef.current - e.clientY; // up = positive
    // Cap resistance at the edges
    const current = heights[snap];
    const proposed = current + dy;
    if (proposed < heights.peek - 40) {
      // dragging below peek — treat as a dismiss gesture
      setDragY(Math.max(heights.peek - 40 - current, dy));
    } else if (proposed > heights.full + 20) {
      setDragY(heights.full + 20 - current);
    } else {
      setDragY(dy);
    }
  };
  const onHandleUp = () => {
    if (dragStartYRef.current == null) return;
    const current = heights[snap] + dragY;
    // Snap to nearest
    const distances = {
      peek: Math.abs(current - heights.peek),
      half: Math.abs(current - heights.half),
      full: Math.abs(current - heights.full),
    };
    const nearest = (Object.entries(distances).sort((a, b) => a[1] - b[1])[0][0]) as SheetSnap;
    // If user pulled below peek, treat as dismiss
    if (current < heights.peek - 20 && onClose) {
      onClose();
    } else {
      setSnap(nearest);
    }
    setDragY(0);
    dragStartYRef.current = null;
  };

  return (
    <div
      ref={sheetRef}
      style={{
        position: 'fixed',
        left: 0,
        right: 0,
        bottom: 0,
        height,
        background: 'rgba(14, 14, 14, 0.98)',
        borderTop: '1px solid #191919',
        boxShadow: '0 -8px 24px rgba(0,0,0,0.6)',
        zIndex: 25,
        display: 'flex',
        flexDirection: 'column',
        paddingBottom: 'env(safe-area-inset-bottom, 0)',
        transition: dragStartYRef.current == null ? 'height 180ms ease-out' : 'none',
        touchAction: 'none',  // so page doesn't scroll while dragging handle
      }}
      // On touch devices, taps within the sheet body should NOT be treated as
      // map clicks. stopPropagation ensures the Pixi canvas beneath isn't hit.
      onPointerDown={(e) => e.stopPropagation()}
    >
      {/* Drag handle */}
      <div
        onPointerDown={onHandleDown}
        onPointerMove={onHandleMove}
        onPointerUp={onHandleUp}
        onPointerCancel={onHandleUp}
        style={{
          padding: '8px 0 4px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexDirection: 'column',
          gap: 2,
          cursor: 'grab',
          touchAction: 'none',
        }}
      >
        <div style={{ width: 44, height: 4, background: '#303030' }} />
        {title && (
          <div style={{
            fontSize: 10, letterSpacing: '0.12em',
            color: '#c8a951', textTransform: 'uppercase', marginTop: 4,
          }}>
            {title}
          </div>
        )}
      </div>

      {/* Content */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '4px 0' }}>
        {children}
      </div>
    </div>
  );
}
