/** Grey placeholder rows while a card loads, so the layout holds still. */
export function SkeletonRows({ rows = 3, className = "" }: { rows?: number; className?: string }) {
  return (
    <div role="status" aria-label="Loading" className={`flex flex-col gap-2 ${className}`}>
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          aria-hidden="true"
          className="h-4 animate-pulse rounded bg-page"
          style={{ width: `${88 - (index % 3) * 14}%` }}
        />
      ))}
    </div>
  );
}

export default SkeletonRows;
