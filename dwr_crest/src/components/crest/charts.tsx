import type { Analysis } from "@/lib/crest/types";

export function HydroChart({ series }: { series: Analysis["breach"]["series"] }) {
  const w = 640;
  const h = 220;
  const padL = 44;
  const padB = 28;
  const padT = 12;
  const padR = 12;
  if (series.length < 2) {
    return <p className="text-muted">No hydrograph.</p>;
  }
  const maxT = Math.max(...series.map((p) => p.tHr), 0.1);
  const maxQ = Math.max(...series.map((p) => p.qCfs), 1);
  const x = (t: number) => padL + (t / maxT) * (w - padL - padR);
  const y = (q: number) => padT + (1 - q / maxQ) * (h - padT - padB);
  const d = series
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.tHr).toFixed(1)},${y(p.qCfs).toFixed(1)}`)
    .join(" ");
  const area = `${d} L${x(series[series.length - 1].tHr).toFixed(1)},${y(0).toFixed(1)} L${x(0).toFixed(1)},${y(0).toFixed(1)} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-56 w-full" role="img" aria-label="Breach hydrograph">
      <line x1={padL} y1={y(0)} x2={w - padR} y2={y(0)} stroke="var(--color-line)" />
      <line x1={padL} y1={padT} x2={padL} y2={y(0)} stroke="var(--color-line)" />
      <path d={area} fill="var(--color-water)" opacity="0.12" />
      <path d={d} fill="none" stroke="var(--color-water)" strokeWidth="2.25" />
      <text x={padL} y={h - 8} fill="var(--color-muted)" fontSize="12">
        0
      </text>
      <text x={w - padR - 36} y={h - 8} fill="var(--color-muted)" fontSize="12">
        {maxT.toFixed(1)} hr
      </text>
      <text x={8} y={padT + 10} fill="var(--color-muted)" fontSize="12">
        cfs
      </text>
    </svg>
  );
}

export function ValleyMap({ flood }: { flood: Analysis["flood"] }) {
  const w = 760;
  const h = 420;
  const { stations, receptors, lengthM } = flood;
  const maxHalf = Math.max(...stations.map((s) => s.halfM), 1);
  const x = (s: number) => 70 + s * (w - 110);
  const y = (latM: number) => h / 2 - (latM / maxHalf) * (h / 2 - 28);
  const left = stations.map((s) => `${x(s.s).toFixed(1)},${y(s.halfM).toFixed(1)}`);
  const right = [...stations].reverse().map((s) => `${x(s.s).toFixed(1)},${y(-s.halfM).toFixed(1)}`);
  const wet = stations.filter((s) => s.depthFt >= 0.1);
  const water =
    wet.length > 1
      ? [
          ...wet.map((s) => `${x(s.s).toFixed(1)},${y(s.halfM * Math.min(1, s.depthFt / Math.max(flood.maxDepthFt, 0.2))).toFixed(1)}`),
          ...[...wet]
            .reverse()
            .map(
              (s) =>
                `${x(s.s).toFixed(1)},${y(-s.halfM * Math.min(1, s.depthFt / Math.max(flood.maxDepthFt, 0.2))).toFixed(1)}`,
            ),
        ].join(" ")
      : "";

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-auto w-full" role="img" aria-label="Schematic valley">
      <rect width={w} height={h} fill="var(--color-sand)" />
      <polygon points={`${left.join(" ")} ${right.join(" ")}`} fill="var(--color-paper)" stroke="var(--color-line)" />
      {water ? <polygon points={water} fill="var(--color-water)" opacity="0.78" /> : null}
      <rect x="36" y={h / 2 - 46} width="36" height="92" fill="var(--color-ink)" />
      <text x="40" y={h / 2 - 54} fill="var(--color-ink)" fontSize="12">
        Dam
      </text>
      <text x={w - 120} y={h - 16} fill="var(--color-muted)" fontSize="12">
        {(lengthM / 1000).toFixed(1)} km downstream
      </text>
      {receptors.map((r) => {
        const half = stations[Math.round(r.station * (stations.length - 1))]?.halfM ?? maxHalf;
        const cx = x(r.station);
        const cy = y(r.lateral * half * 0.72);
        return (
          <g key={r.id}>
            <circle
              cx={cx}
              cy={cy}
              r={r.kind === "road" ? 5 : 7}
              fill={r.unresolved ? "none" : r.flooded ? "var(--color-card)" : "var(--color-ink)"}
              stroke="var(--color-ink)"
              strokeWidth="1.5"
              strokeDasharray={r.unresolved ? "3 2" : undefined}
            />
            <text x={cx + 10} y={cy + 4} fill="var(--color-ink)" fontSize="12">
              {r.unresolved ? `${r.name} (unresolved, ${r.distanceKm.toFixed(1)} km)` : r.name}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
