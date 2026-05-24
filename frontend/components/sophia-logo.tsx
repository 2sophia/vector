/**
 * Sophia logo — inline SVG with the canonical Sophia indigo→purple radial gradient.
 * Kept inline so we can size it via React props without a roundtrip through /public.
 */

export function SophiaLogo({
  size = 32,
  className,
}: {
  size?: number;
  className?: string;
}) {
  // Unique gradient id per size — avoids <defs> collisions when multiple
  // instances render on the same page.
  const gradientId = `sophia-logo-grad-${size}`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label="Sophia"
      className={className}
    >
      <defs>
        <radialGradient id={gradientId} cx="0.3" cy="0.3" r="0.8">
          <stop offset="0%" stopColor="#3B82F6" />
          <stop offset="100%" stopColor="#9333EA" />
        </radialGradient>
      </defs>
      <circle cx="24" cy="24" r="24" fill={`url(#${gradientId})`} />
      <circle cx="24" cy="24" r="12" fill="white" fillOpacity="0.9" />
    </svg>
  );
}
