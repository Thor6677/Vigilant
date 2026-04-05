interface Props {
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFitAll: () => void;
}

export function MapToolbar({ onZoomIn, onZoomOut, onFitAll }: Props) {
  const btnStyle: React.CSSProperties = {
    width: 32,
    height: 32,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: 'rgba(14, 14, 14, 0.9)',
    border: '1px solid #191919',
    color: '#474747',
    fontSize: 16,
    cursor: 'pointer',
    fontFamily: "'JetBrains Mono', monospace",
  };

  return (
    <div style={{
      position: 'absolute',
      bottom: 48,
      right: 12,
      display: 'flex',
      flexDirection: 'column',
      gap: 2,
      zIndex: 30,
    }}>
      <button style={btnStyle} onClick={onZoomIn} title="Zoom in (+)">+</button>
      <button style={btnStyle} onClick={onZoomOut} title="Zoom out (-)">−</button>
      <button style={btnStyle} onClick={onFitAll} title="Fit all (Home)">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="2" y="2" width="12" height="12" />
          <line x1="8" y1="5" x2="8" y2="11" />
          <line x1="5" y1="8" x2="11" y2="8" />
        </svg>
      </button>
    </div>
  );
}
