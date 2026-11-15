"use client";

import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { CANVAS, EDGES, KINDS, LANES, NODES, RULES_BOX, type FlowEdge, type Kind } from "./flow-data";
import "./flow-map.css";

// Plain CSS, not a CSS module: next.config.ts routes every *.css file through the Tailwind
// loader as global CSS. Every class in flow-map.css is prefixed "fm-" to keep it local.
const styles = new Proxy({} as Record<string, string>, { get: (_, name) => `fm-${String(name)}` });

const HALF = 28; // half a node tile
const FIT_MIN_WIDTH = 960; // below this the board scrolls instead of shrinking
const color = (kind: Kind) => `var(--k-${kind})`;

const ICONS: Record<Kind | "fork", ReactNode> = {
  start: <path d="M2 -11 L-6 2 L0 2 L-2 11 L6 -2 L0 -2 Z" />,
  request: <path d="M-5 -6 L-11 0 L-5 6 M5 -6 L11 0 L5 6 M2 -8 L-2 8" />,
  ai: <path d="M0 -11 C1 -3 3 -1 11 0 C3 1 1 3 0 11 C-1 3 -3 1 -11 0 C-3 -1 -1 -3 0 -11 Z" />,
  rules: (
    <>
      <path d="M0 -11 L9.5 -5.5 L9.5 5.5 L0 11 L-9.5 5.5 L-9.5 -5.5 Z" />
      <circle r="2.5" />
    </>
  ),
  data: (
    <>
      <ellipse cy="-7" rx="9" ry="3.5" />
      <path d="M-9 -7 L-9 7 A9 3.5 0 0 0 9 7 L9 -7 M-9 0 A9 3.5 0 0 0 9 0" />
    </>
  ),
  background: (
    <>
      <circle r="10" />
      <path d="M0 -6 L0 0 L5 3" />
    </>
  ),
  people: (
    <>
      <circle cy="-4" r="4.5" />
      <path d="M-9 11 C-9 2 9 2 9 11" />
    </>
  ),
  provider: (
    <>
      <rect x="-11" y="-7.5" width="22" height="15" rx="2.5" />
      <path d="M-11 -2 L11 -2 M-7 3.5 L-2 3.5" />
    </>
  ),
  stop: (
    <>
      <circle r="10" />
      <path d="M-7 7 L7 -7" />
    </>
  ),
  fork: <path d="M-10 0 L-2 0 M-2 0 L9 -8 M-2 0 L9 0 M-2 0 L9 8" />,
};

type Point = [number, number];

function rounded(points: Point[], radius: number): string {
  let d = `M${points[0][0]} ${points[0][1]}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [x0, y0] = points[i - 1];
    const [x1, y1] = points[i];
    const [x2, y2] = points[i + 1];
    const a = Math.min(radius, Math.hypot(x1 - x0, y1 - y0) / 2);
    const b = Math.min(radius, Math.hypot(x2 - x1, y2 - y1) / 2);
    const [ux, uy] = [Math.sign(x1 - x0), Math.sign(y1 - y0)];
    const [vx, vy] = [Math.sign(x2 - x1), Math.sign(y2 - y1)];
    d += ` L${x1 - ux * a} ${y1 - uy * a} Q${x1} ${y1} ${x1 + vx * b} ${y1 + vy * b}`;
  }
  const last = points[points.length - 1];
  return `${d} L${last[0]} ${last[1]}`;
}

function wire(edge: FlowEdge) {
  const a = NODES[edge.from];
  const b = NODES[edge.to];
  const [x1, y1] = [a.x + HALF, a.y + (edge.out ?? 0)];
  const [x2, y2] = [b.x - HALF, b.y];
  if (edge.via !== undefined) {
    const points: Point[] = [[x1, y1], [x1 + 16, y1], [x1 + 16, edge.via], [x2 - 16, edge.via], [x2 - 16, y2], [x2, y2]];
    return { d: rounded(points, 10), mid: [(x1 + x2) / 2, edge.via] as Point, end: [x2, y2] as Point };
  }
  const dx = Math.max(30, (x2 - x1) / 2);
  const [c1x, c2x] = [x1 + dx, x2 - dx];
  return {
    d: `M${x1} ${y1} C${c1x} ${y1} ${c2x} ${y2} ${x2} ${y2}`,
    mid: [(x1 + 3 * c1x + 3 * c2x + x2) / 8, (y1 + 3 * y1 + 3 * y2 + y2) / 8] as Point,
    end: [x2, y2] as Point,
  };
}

const WIRES = EDGES.map((edge) => ({ edge, ...wire(edge) }));

function Tile({ kind }: { kind: Kind }) {
  if (kind === "start") {
    // A start node has a rounded front, like a trigger in a workflow editor.
    return (
      <path
        className={styles.tile}
        d={`M${-HALF + 22} ${-HALF} H${HALF - 8} A8 8 0 0 1 ${HALF} ${-HALF + 8} V${HALF - 8} A8 8 0 0 1 ${HALF - 8} ${HALF} H${-HALF + 22} A22 ${HALF} 0 0 1 ${-HALF + 22} ${-HALF} Z`}
      />
    );
  }
  return <rect className={styles.tile} x={-HALF} y={-HALF} width={2 * HALF} height={2 * HALF} rx={9} />;
}

export function FlowMap() {
  const [selected, setSelected] = useState("engine");
  const [zoom, setZoom] = useState<number | null>(null); // null = fit to the board
  const svgRef = useRef<SVGSVGElement>(null);

  const node = NODES[selected];
  const into = EDGES.filter((e) => e.to === selected).map((e) => e.from);
  const outOf = EDGES.filter((e) => e.from === selected).map((e) => e.to);
  const touches = (e: FlowEdge) => e.from === selected || e.to === selected;

  const currentZoom = () => zoom ?? (svgRef.current?.getBoundingClientRect().width ?? CANVAS.width) / CANVAS.width;
  const onKey = (id: string) => (event: KeyboardEvent) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelected(id);
    }
  };

  const jumpList = (ids: string[]) =>
    ids.length ? (
      <ul>
        {ids.map((id) => (
          <li key={id}>
            <button type="button" onClick={() => setSelected(id)}>
              {NODES[id].name}
            </button>
          </li>
        ))}
      </ul>
    ) : (
      <ul>
        <li className={styles.none}>nothing</li>
      </ul>
    );

  return (
    <div className={styles.map}>
      <div className={styles.legend} aria-label="Legend">
        {(Object.keys(KINDS) as Kind[]).map((kind) => (
          <span key={kind}>
            <i className={styles.swatch} style={{ background: color(kind) }} />
            {KINDS[kind]}
          </span>
        ))}
        <span>
          <i className={styles.dashKey} />
          Waits for time or a person
        </span>
      </div>

      <figure className="m-0">
        <div className={styles.board}>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${CANVAS.width} ${CANVAS.height}`}
            style={zoom === null ? { width: "100%", minWidth: FIT_MIN_WIDTH } : { width: Math.round(CANVAS.width * zoom) }}
            role="img"
            aria-label="How a payment request is decided by fixed rules, how the person is asked when needed, and how a dispute becomes a checked, signed-off evidence pack."
          >
            {LANES.map((lane) => (
              <g key={lane.title}>
                <rect className={styles.laneBg} x={10} y={lane.y} width={CANVAS.width - 20} height={lane.height} rx={12} />
                <text className={styles.laneTitle} x={CANVAS.width - 28} y={lane.y + 24} textAnchor="end">
                  {lane.title}
                </text>
              </g>
            ))}
            <rect className={styles.rulesBox} x={RULES_BOX.x} y={RULES_BOX.y} width={RULES_BOX.width} height={RULES_BOX.height} rx={14} />
            <text className={styles.rulesTitle} x={RULES_BOX.x + 12} y={RULES_BOX.y + 18}>
              {RULES_BOX.title}
            </text>

            {WIRES.map(({ edge, d, end }, i) => {
              const hot = touches(edge) ? ` ${styles.hot}` : "";
              const [ex, ey] = end;
              return (
                <g key={i}>
                  <path className={`${styles.wire}${edge.waits ? ` ${styles.waits}` : ""}${hot}`} d={d} />
                  <path className={`${styles.arrow}${hot}`} d={`M${ex - 7} ${ey - 4.5} L${ex - 1} ${ey} L${ex - 7} ${ey + 4.5} Z`} />
                </g>
              );
            })}
            {WIRES.map(({ edge, mid }, i) => {
              if (!edge.label) return null;
              const [x, y] = [mid[0] + (edge.nudge?.[0] ?? 0), mid[1] + (edge.nudge?.[1] ?? 0)];
              const width = edge.label.length * 5.6 + 10;
              return (
                <g key={i} className={`${styles.wireLabel}${touches(edge) ? ` ${styles.hot}` : ""}`}>
                  <rect x={x - width / 2} y={y - 8} width={width} height={16} rx={4} />
                  <text x={x} y={y + 3.5} textAnchor="middle">
                    {edge.label}
                  </text>
                </g>
              );
            })}

            {Object.entries(NODES).map(([id, n]) => (
              <g
                key={id}
                className={`${styles.node}${id === selected ? ` ${styles.selected}` : ""}`}
                transform={`translate(${n.x} ${n.y})`}
                tabIndex={0}
                role="button"
                aria-label={`${n.name}: ${n.short}`}
                aria-pressed={id === selected}
                onClick={() => setSelected(id)}
                onKeyDown={onKey(id)}
              >
                <title>{n.about}</title>
                <Tile kind={n.kind} />
                <rect x={-17} y={-17} width={34} height={34} rx={8} fill={color(n.kind)} fillOpacity={0.13} />
                <g className={styles.icon} stroke={color(n.kind)}>
                  {ICONS[id === "answer" ? "fork" : n.kind]}
                </g>
                {n.kind !== "start" && <circle className={styles.port} cx={-HALF} cy={0} r={4} />}
                {(id === "answer" ? [-16, 0, 16] : [0]).map((y) => (
                  <circle key={y} className={styles.port} cx={HALF} cy={y} r={4} />
                ))}
                <text className={styles.name} y={HALF + 18} textAnchor="middle">
                  {n.name}
                </text>
                <text className={styles.short} y={HALF + 32} textAnchor="middle">
                  {n.short}
                </text>
              </g>
            ))}
          </svg>
          <div className={styles.zoom}>
            <button type="button" aria-label="Zoom out" onClick={() => setZoom(Math.max(0.5, currentZoom() - 0.15))}>
              −
            </button>
            <button type="button" aria-label="Zoom in" onClick={() => setZoom(Math.min(1.6, currentZoom() + 0.15))}>
              +
            </button>
            <button type="button" onClick={() => setZoom(null)}>
              Fit
            </button>
          </div>
        </div>
        <figcaption className={`${styles.caption} mt-2`}>
          The green dashed box is where each payment is decided. No AI works inside it. The AI
          only gives a hint beforehand, and if it is slow or unsure, the person is simply asked.
          The payment request never waits for the background work in lanes 2 and 3.
        </figcaption>
      </figure>

      <section className={styles.panel} aria-live="polite">
        <div>
          <h2>
            {node.name}
            <span className={styles.chip} style={{ color: color(node.kind) }}>
              {KINDS[node.kind]}
            </span>
          </h2>
          <p>{node.about}</p>
          <div className={styles.code}>
            In the code: <code>{node.code}</code>
          </div>
        </div>
        <div className={styles.links}>
          <h3>Comes from</h3>
          {jumpList(into)}
          <h3>Leads to</h3>
          {jumpList(outOf)}
        </div>
      </section>
    </div>
  );
}
