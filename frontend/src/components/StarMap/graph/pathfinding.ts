import Graph from 'graphology';
import { bidirectional as dijkstraBidirectional } from 'graphology-shortest-path/dijkstra';
import type { RoutePreference } from '../types';

/**
 * Apply routing preference weights to the graph edges.
 * Returns a cleanup function to restore original weights.
 */
function applyWeights(
  graph: Graph,
  preference: RoutePreference,
  avoidSystems?: Set<number>,
): () => void {
  const originalWeights = new Map<string, number>();

  graph.forEachEdge((edge, _attrs, src, dst) => {
    const srcSec = graph.getNodeAttribute(src, 'sec') as number;
    const dstSec = graph.getNodeAttribute(dst, 'sec') as number;
    const minSec = Math.min(srcSec, dstSec);

    let weight = 1;

    switch (preference) {
      case 'highsec':
        if (minSec < 0.5) weight = minSec < 0 ? 50 : 10;
        break;
      case 'lowsec':
        if (minSec >= 0.5) weight = 5;
        break;
      case 'nullsec':
        if (minSec >= 0.5) weight = 10;
        else if (minSec > 0) weight = 3;
        break;
    }

    // Heavily penalize avoided systems
    if (avoidSystems) {
      const srcId = Number(src);
      const dstId = Number(dst);
      if (avoidSystems.has(srcId) || avoidSystems.has(dstId)) {
        weight = 10000;
      }
    }

    if (weight !== 1) {
      originalWeights.set(edge, graph.getEdgeAttribute(edge, 'weight') ?? 1);
      graph.setEdgeAttribute(edge, 'weight', weight);
    }
  });

  return () => {
    for (const [edge, w] of originalWeights) {
      if (graph.hasEdge(edge)) {
        graph.setEdgeAttribute(edge, 'weight', w);
      }
    }
  };
}

export function findRoute(
  graph: Graph,
  originId: number,
  destId: number,
  preference: RoutePreference = 'shortest',
  avoidSystems?: Set<number>,
): number[] | null {
  const srcKey = String(originId);
  const dstKey = String(destId);

  if (!graph.hasNode(srcKey) || !graph.hasNode(dstKey)) return null;

  const restore = applyWeights(graph, preference, avoidSystems);

  try {
    // Use Dijkstra (weighted) so the per-edge weights set by applyWeights
    // actually influence the path. The unweighted bidirectional BFS that
    // used to be imported here ignored edge weights entirely, which silently
    // disabled the highsec / lowsec / nullsec preferences.
    const path = dijkstraBidirectional(graph, srcKey, dstKey, 'weight');
    if (!path) return null;
    return path.map(Number);
  } catch {
    return null;
  } finally {
    restore();
  }
}
