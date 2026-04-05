import Graph from 'graphology';
import type { SystemData, Edge } from '../types';

export function buildGraph(
  systems: SystemData[],
  edges: Edge[],
): Graph {
  const graph = new Graph({ type: 'undirected', allowSelfLoops: false });

  for (const sys of systems) {
    graph.addNode(String(sys.id), {
      x: sys.x,
      y: sys.y,
      sec: sys.sec,
      name: sys.name,
      regId: sys.regId,
      conId: sys.conId,
    });
  }

  for (const [src, dst] of edges) {
    const srcKey = String(src);
    const dstKey = String(dst);
    if (graph.hasNode(srcKey) && graph.hasNode(dstKey)) {
      graph.addEdge(srcKey, dstKey, { weight: 1 });
    }
  }

  return graph;
}
