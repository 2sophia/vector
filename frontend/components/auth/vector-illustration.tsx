"use client";

/**
 * Glass-style vector-space mockup for the /auth hero panel.
 * Three pastel clusters of embeddings, one query point at the centre,
 * dashed lines to its k-nearest neighbours, and a search radius ring.
 *
 * Animated with native SMIL (`<animate>`): no framer-motion, no CSS,
 * no client-side recompute — pure declarative SVG.
 *  - Cluster dots breathe (staggered fill-opacity).
 *  - Energy flows along the dashed neighbour lines toward the query.
 *  - The search radii pulse like a slow sonar.
 *  - The query halo expands/contracts.
 *  - Corner sparkles twinkle.
 */
export function VectorSpaceIllustration() {
  const clusterA = [
    { x: 70, y: 80 },
    { x: 90, y: 65 },
    { x: 100, y: 95 },
    { x: 75, y: 105 },
    { x: 115, y: 80 },
  ];
  const clusterB = [
    { x: 360, y: 100 },
    { x: 385, y: 85 },
    { x: 395, y: 120 },
    { x: 410, y: 100 },
    { x: 375, y: 130 },
  ];
  const clusterC = [
    { x: 230, y: 250 },
    { x: 255, y: 270 },
    { x: 220, y: 285 },
    { x: 270, y: 245 },
    { x: 245, y: 300 },
  ];

  const query = { x: 240, y: 170 };
  const neighbours = [
    { x: 115, y: 80 },
    { x: 360, y: 100 },
    { x: 230, y: 250 },
  ];

  // Stagger helpers — slightly different begin offsets so dots don't
  // breathe in sync (looks more alive).
  const stagger = (i: number) => `${(i * 0.37) % 2.5}s`;

  return (
    <svg
      viewBox="0 0 480 360"
      fill="none"
      className="w-full h-full"
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <radialGradient id="ring-grad" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="white" stopOpacity="0.18" />
          <stop offset="70%" stopColor="white" stopOpacity="0.04" />
          <stop offset="100%" stopColor="white" stopOpacity="0" />
        </radialGradient>
        <filter id="soft-glow">
          <feGaussianBlur stdDeviation="3" />
        </filter>
      </defs>

      {/* Faint dotted grid background */}
      <g fill="white" fillOpacity="0.06">
        {Array.from({ length: 8 }).map((_, row) =>
          Array.from({ length: 11 }).map((__, col) => (
            <circle
              key={`g-${row}-${col}`}
              cx={40 + col * 40}
              cy={30 + row * 40}
              r="1"
            />
          ))
        )}
      </g>

      {/* Outer search radius — slow sonar pulse */}
      <circle
        cx={query.x}
        cy={query.y}
        r="120"
        fill="url(#ring-grad)"
        stroke="white"
        strokeOpacity="0.25"
        strokeDasharray="4 4"
      >
        <animate
          attributeName="r"
          values="118;124;118"
          dur="4.5s"
          repeatCount="indefinite"
        />
        <animate
          attributeName="stroke-opacity"
          values="0.22;0.35;0.22"
          dur="4.5s"
          repeatCount="indefinite"
        />
      </circle>

      {/* Inner search radius — faster, smaller breath */}
      <circle
        cx={query.x}
        cy={query.y}
        r="75"
        stroke="white"
        strokeOpacity="0.18"
        strokeDasharray="2 5"
      >
        <animate
          attributeName="r"
          values="72;80;72"
          dur="3s"
          repeatCount="indefinite"
        />
      </circle>

      {/* Dashed lines from query to k-nearest neighbours — energy flow */}
      <g stroke="white" strokeOpacity="0.55" strokeWidth="1.4" strokeDasharray="6 6">
        {neighbours.map((n, i) => (
          <line key={i} x1={query.x} y1={query.y} x2={n.x} y2={n.y}>
            <animate
              attributeName="stroke-dashoffset"
              from="0"
              to="-12"
              dur="1.4s"
              begin={`${i * 0.25}s`}
              repeatCount="indefinite"
            />
            <animate
              attributeName="stroke-opacity"
              values="0.4;0.75;0.4"
              dur="2.2s"
              begin={`${i * 0.3}s`}
              repeatCount="indefinite"
            />
          </line>
        ))}
      </g>

      {/* Cluster A */}
      <g>
        {clusterA.map((p, i) => (
          <circle
            key={`a-${i}`}
            cx={p.x}
            cy={p.y}
            r={6 + (i % 3)}
            fill="white"
            stroke="white"
            strokeOpacity="0.55"
          >
            <animate
              attributeName="fill-opacity"
              values="0.3;0.55;0.3"
              dur="3.2s"
              begin={stagger(i)}
              repeatCount="indefinite"
            />
          </circle>
        ))}
        <text
          x="90"
          y="135"
          fontSize="10"
          fill="white"
          fillOpacity="0.65"
          fontFamily="ui-sans-serif, system-ui"
          textAnchor="middle"
        >
          docs · A
        </text>
      </g>

      {/* Cluster B */}
      <g>
        {clusterB.map((p, i) => (
          <circle
            key={`b-${i}`}
            cx={p.x}
            cy={p.y}
            r={6 + (i % 3)}
            fill="white"
            stroke="white"
            strokeOpacity="0.5"
          >
            <animate
              attributeName="fill-opacity"
              values="0.28;0.5;0.28"
              dur="3.5s"
              begin={stagger(i + 2)}
              repeatCount="indefinite"
            />
          </circle>
        ))}
        <text
          x="385"
          y="160"
          fontSize="10"
          fill="white"
          fillOpacity="0.6"
          fontFamily="ui-sans-serif, system-ui"
          textAnchor="middle"
        >
          docs · B
        </text>
      </g>

      {/* Cluster C */}
      <g>
        {clusterC.map((p, i) => (
          <circle
            key={`c-${i}`}
            cx={p.x}
            cy={p.y}
            r={6 + (i % 3)}
            fill="white"
            stroke="white"
            strokeOpacity="0.5"
          >
            <animate
              attributeName="fill-opacity"
              values="0.26;0.48;0.26"
              dur="3.8s"
              begin={stagger(i + 4)}
              repeatCount="indefinite"
            />
          </circle>
        ))}
        <text
          x="245"
          y="325"
          fontSize="10"
          fill="white"
          fillOpacity="0.6"
          fontFamily="ui-sans-serif, system-ui"
          textAnchor="middle"
        >
          docs · C
        </text>
      </g>

      {/* Query halo — breathes wider than the dot */}
      <circle
        cx={query.x}
        cy={query.y}
        r="18"
        fill="white"
        fillOpacity="0.25"
        filter="url(#soft-glow)"
      >
        <animate
          attributeName="r"
          values="16;22;16"
          dur="2.4s"
          repeatCount="indefinite"
        />
        <animate
          attributeName="fill-opacity"
          values="0.18;0.35;0.18"
          dur="2.4s"
          repeatCount="indefinite"
        />
      </circle>

      {/* Query inner solid dot */}
      <circle cx={query.x} cy={query.y} r="10" fill="white" fillOpacity="0.9" />

      {/* Query ring — slow expanding ping */}
      <circle
        cx={query.x}
        cy={query.y}
        r="14"
        fill="none"
        stroke="white"
        strokeWidth="1.5"
      >
        <animate
          attributeName="r"
          values="14;28;14"
          dur="3s"
          repeatCount="indefinite"
        />
        <animate
          attributeName="stroke-opacity"
          values="0.85;0;0.85"
          dur="3s"
          repeatCount="indefinite"
        />
      </circle>

      <text
        x={query.x}
        y={query.y - 28}
        fontSize="11"
        fill="white"
        fillOpacity="0.95"
        fontFamily="ui-sans-serif, system-ui"
        textAnchor="middle"
        fontWeight="600"
      >
        query
      </text>

      {/* Coordinate axes hint */}
      <g stroke="white" strokeOpacity="0.25" strokeWidth="1.2">
        <line x1="30" y1="330" x2="30" y2="280" />
        <line x1="30" y1="330" x2="80" y2="330" />
        <polygon
          points="30,275 27,283 33,283"
          fill="white"
          fillOpacity="0.4"
          stroke="none"
        />
        <polygon
          points="85,330 77,327 77,333"
          fill="white"
          fillOpacity="0.4"
          stroke="none"
        />
      </g>
      <text
        x="20"
        y="280"
        fontSize="9"
        fill="white"
        fillOpacity="0.55"
        fontFamily="ui-sans-serif, system-ui"
      >
        d₂
      </text>
      <text
        x="85"
        y="345"
        fontSize="9"
        fill="white"
        fillOpacity="0.55"
        fontFamily="ui-sans-serif, system-ui"
      >
        d₁
      </text>

      {/* Corner sparkles — twinkle */}
      <g fill="white">
        <path
          d="M 440 40 L 442 46 L 448 48 L 442 50 L 440 56 L 438 50 L 432 48 L 438 46 Z"
          fillOpacity="0.55"
        >
          <animate
            attributeName="fill-opacity"
            values="0.3;0.75;0.3"
            dur="2.6s"
            repeatCount="indefinite"
          />
        </path>
        <circle cx="35" cy="40" r="2" fillOpacity="0.5">
          <animate
            attributeName="fill-opacity"
            values="0.2;0.7;0.2"
            dur="2.1s"
            begin="0.5s"
            repeatCount="indefinite"
          />
        </circle>
        <circle cx="450" cy="320" r="1.8" fillOpacity="0.45">
          <animate
            attributeName="fill-opacity"
            values="0.2;0.6;0.2"
            dur="2.8s"
            begin="1.2s"
            repeatCount="indefinite"
          />
        </circle>
      </g>
    </svg>
  );
}
