export interface AmbientKillSourceSimulate { type: 'simulate' }
export interface AmbientKillSourcePoll { type: 'poll'; url: string; intervalMs?: number }
export type AmbientKillSource = AmbientKillSourceSimulate | AmbientKillSourcePoll;

export interface AmbientOptions {
  systemsUrl?: string;
  killSource?: AmbientKillSource;
  minWidth?: number;
  fpsCap?: number;
  speed?: number;
}

export interface AmbientHandle {
  flare?(systemId: number): void;
  destroy(): void;
}

/**
 * Mount into <body> or a plain ancestor — position:fixed re-anchors under transformed/filtered ancestors (the glass styles use backdrop-filter).
 */
export function mount(el: HTMLElement, options?: AmbientOptions): AmbientHandle;
export function allianceColor(id: number): [number, number, number];
export function normalizeSystems(
  raw: Array<{ id: number; name: string; x3: number; y3: number; z3: number }>
): Array<{ id: number; name: string; x: number; y: number; z: number }>;
