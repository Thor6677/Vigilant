import Graph from 'graphology';
import type { SystemData, Edge } from '../types';

export interface GraphBundle {
  graph: Graph;
  adjacency: Map<number, Set<number>>;
}

export function buildGraph(
  systems: SystemData[],
  edges: Edge[],
): GraphBundle {
  const graph = new Graph({ type: 'undirected', allowSelfLoops: false });
  const adjacency = new Map<number, Set<number>>();

  for (const sys of systems) {
    graph.addNode(String(sys.id), {
      x: sys.x,
      y: sys.y,
      sec: sys.sec,
      name: sys.name,
      regId: sys.regId,
      conId: sys.conId,
    });
    adjacency.set(sys.id, new Set());
  }

  for (const [src, dst] of edges) {
    const srcKey = String(src);
    const dstKey = String(dst);
    if (graph.hasNode(srcKey) && graph.hasNode(dstKey)) {
      graph.addEdge(srcKey, dstKey, { weight: 1 });
      adjacency.get(src)?.add(dst);
      adjacency.get(dst)?.add(src);
    }
  }

  return { graph, adjacency };
}
