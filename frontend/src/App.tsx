import { useMapData } from './components/StarMap/useMapData';
import { StarMap } from './components/StarMap/StarMap';

export default function App() {
  const { data, loading, error } = useMapData();

  if (error) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        width: '100%', height: '100%', color: '#ef4444',
        fontFamily: 'DM Sans, sans-serif', fontSize: 16,
      }}>
        Failed to load map data: {error}
      </div>
    );
  }

  if (loading || !data) {
    return (
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
        width: '100%', height: '100%', fontFamily: 'DM Sans, sans-serif',
        color: '#6a7a8a', gap: 16,
      }}>
        <div style={{
          width: 40, height: 40, border: '3px solid #2a3a5a',
          borderTopColor: '#00d4ff', borderRadius: '50%',
          animation: 'spin 1s linear infinite',
        }} />
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
        Loading star map data...
      </div>
    );
  }

  return <StarMap data={data} />;
}
