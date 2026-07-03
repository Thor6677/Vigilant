export interface SkeletonProps {
  /** number of shimmer bars (default 3) */
  lines?: number;
  /** CSS width of the last line, e.g. '60%' */
  lastLineWidth?: string;
}

export function Skeleton({ lines = 3, lastLineWidth = '60%' }: SkeletonProps) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="b-skeleton" style={i === lines - 1 ? { width: lastLineWidth } : undefined} />
      ))}
    </div>
  );
}
