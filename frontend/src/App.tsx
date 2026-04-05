import { useMapData } from './components/StarMap/useMapData';
import { StarMap } from './components/StarMap/StarMap';

export default function App() {
  const { data, loading, error } = useMapData();

  if (error) {
    return (
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        width: '100%', height: '100%', color: '#cc3333',
        fontFamily: "'JetBrains Mono', monospace", fontSize: 12,
      }}>
        Failed to load map data: {error}
      </div>
    );
  }

  if (loading || !data) {
    return null; // Template shows the loading spinner
  }

  return <StarMap data={data} />;
}
