import { useMapData } from './components/StarMap/useMapData';
import { StarMap } from './components/StarMap/StarMap';

// Read the space ('k' | 'w') from the #map-root data attribute set by the
// page template. Defaults to 'k' (k-space) for the legacy /map page.
function readSpace(): 'k' | 'w' {
  const root = document.getElementById('map-root');
  return root?.dataset.space === 'w' ? 'w' : 'k';
}

export default function App() {
  const space = readSpace();
  const { data, loading, error } = useMapData(space);

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

  return <StarMap data={data} space={space} />;
}
