interface Props {
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitAll: () => void;
}

export function MapToolbar({ onZoomIn, onZoomOut, onFitAll }: Props) {
  const btnStyle: React.CSSProperties = {
    width: 36,
    height: 36,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: 'rgba(10, 14, 30, 0.85)',
    border: '1px solid #2a3a5a',
    borderRadius: 6,
    color: '#8a9ab0',
    fontSize: 18,
    cursor: 'pointer',
    fontFamily: 'monospace',
  };

  return (
    <div style={{
      position: 'absolute',
      bottom: 16,
      right: 16,
      display: 'flex',
      flexDirection: 'column',
      gap: 4,
      zIndex: 30,
    }}>
      <button style={btnStyle} onClick={onZoomIn} title="Zoom in (+)">+</button>
      <button style={btnStyle} onClick={onZoomOut} title="Zoom out (-)">-</button>
      <button style={btnStyle} onClick={onFitAll} title="Fit all (Home)">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="2" y="2" width="12" height="12" rx="1" />
          <line x1="8" y1="5" x2="8" y2="11" />
          <line x1="5" y1="8" x2="11" y2="8" />
        </svg>
      </button>
    </div>
  );
}
