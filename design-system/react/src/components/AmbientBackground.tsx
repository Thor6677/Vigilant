import { useEffect, useRef } from 'react';
import { mount, type AmbientOptions } from '../../../ambient/vigilant-ambient.js';

/**
 * Props for {@link AmbientBackground}.
 *
 * The module's canvas is `position:fixed`, so the wrapper div (or any of its
 * ancestors) must NOT have a `transform`, `filter`, or `backdrop-filter` set
 * — any of those establish a new containing block and will re-anchor the
 * fixed canvas to that ancestor instead of the viewport.
 */
export interface AmbientBackgroundProps extends AmbientOptions {
  className?: string;
}

/**
 * Mounts the dependency-free `vigilant-ambient` canvas module (New Eden
 * flythrough, live sov colors, kill blips) into a wrapper div on mount, and
 * tears it down on unmount. Mounts once with the options snapshot taken at
 * first render — later prop changes are not applied (no remount-on-change).
 */
export function AmbientBackground({ className, ...options }: AmbientBackgroundProps) {
  const ref = useRef<HTMLDivElement>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    if (!ref.current) return;
    const handle = mount(ref.current, optionsRef.current);
    return () => handle.destroy();
  }, []);

  return <div ref={ref} className={className} aria-hidden="true" />;
}
