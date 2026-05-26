"use client";

/**
 * GraphViewer — visualizzatore force-graph (2D/3D) del knowledge graph.
 *
 * Basato su react-force-graph e adattato ai label di sophia-vector
 * (Document / Section / Chunk / Entity / Content). Componente
 * "puro": riceve `graphData` ({nodes, links}) via props, nessun accoppiamento
 * ad auth/store/API. Va caricato via `dynamic(..., { ssr: false })` perché
 * react-force-graph e three usano window/canvas/WebGL.
 */

import { useRef, useEffect, useCallback, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import * as THREE from "three";
import { Loader2 } from "lucide-react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const ForceGraph2D = dynamic(() => import("react-force-graph-2d"), { ssr: false }) as any;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const ForceGraph3D = dynamic(() => import("react-force-graph-3d"), { ssr: false }) as any;

// Colori per label del NOSTRO grafo (palette coerente col brand indigo)
const NODE_COLORS: Record<string, string> = {
  Document: "#6366f1", // indigo — il documento (brand)
  Section: "#3b82f6", // blue — sezione/heading
  Chunk: "#06b6d4", // cyan — il chunk indicizzato
  Entity: "#22c55e", // green — entità estratta (GLiNER)
  Content: "#f97316", // orange — contenuto condiviso (curation/boilerplate)
};
const NODE_SIZES: Record<string, number> = {
  Document: 8,
  Section: 5,
  Chunk: 3,
  Entity: 4,
  Content: 4,
};
const DEFAULT_COLOR = "#94a3b8";
const DEFAULT_SIZE = 4;

export type GraphNode = {
  id: string;
  label: string;
  name: string;
  properties?: Record<string, unknown>;
  // popolati a runtime dalla simulazione
  x?: number;
  y?: number;
  z?: number;
};
export type GraphLink = {
  source: string | GraphNode;
  target: string | GraphNode;
  type?: string;
};
export type GraphData = { nodes: GraphNode[]; links: GraphLink[] };

function getLinkId(l: GraphLink): { src: string; tgt: string } {
  const src = typeof l.source === "object" ? l.source.id : l.source;
  const tgt = typeof l.target === "object" ? l.target.id : l.target;
  return { src, tgt };
}

export function GraphViewer({
  graphData,
  onNodeClick,
  selectedNode,
  width,
  height,
  visibleLabels,
  connectedOnly,
  view3D = false,
}: {
  graphData: GraphData | null;
  onNodeClick?: (node: GraphNode | null) => void;
  selectedNode?: string | null;
  width?: number;
  height?: number;
  visibleLabels?: Set<string>;
  connectedOnly?: boolean;
  view3D?: boolean;
}) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(undefined);
  const [settling, setSettling] = useState(true);

  // Filtra per label visibili + (opz.) solo nodi connessi
  const baseData = useMemo<GraphData>(() => {
    if (!graphData) return { nodes: [], links: [] };

    let nodes = graphData.nodes;
    if (visibleLabels && visibleLabels.size > 0) {
      nodes = nodes.filter((n) => visibleLabels.has(n.label));
    }
    const nodeIds = new Set(nodes.map((n) => n.id));
    const links = graphData.links.filter((l) => {
      const { src, tgt } = getLinkId(l);
      return nodeIds.has(src) && nodeIds.has(tgt);
    });

    if (connectedOnly) {
      const connected = new Set<string>();
      for (const l of links) {
        const { src, tgt } = getLinkId(l);
        connected.add(src);
        connected.add(tgt);
      }
      nodes = nodes.filter((n) => connected.has(n.id));
    }

    return { nodes, links };
  }, [graphData, visibleLabels, connectedOnly]);

  // Vicinato del nodo selezionato (per evidenziare/spegnere il resto)
  const neighborSet = useMemo<Set<string> | null>(() => {
    if (!selectedNode || !baseData.links.length) return null;
    const neighbors = new Set<string>([selectedNode]);
    for (const l of baseData.links) {
      const { src, tgt } = getLinkId(l);
      if (src === selectedNode) neighbors.add(tgt);
      if (tgt === selectedNode) neighbors.add(src);
    }
    return neighbors;
  }, [selectedNode, baseData]);

  const filteredData = baseData;

  // Loader durante il settling della simulazione
  useEffect(() => {
    if (graphData?.nodes?.length) {
      setSettling(true);
      const timer = setTimeout(() => setSettling(false), 4000);
      return () => clearTimeout(timer);
    }
  }, [graphData, view3D]);

  // Centra/zooma sul nodo selezionato
  useEffect(() => {
    if (!fgRef.current || !selectedNode) return;
    const node = filteredData.nodes.find((n) => n.id === selectedNode);
    if (!node || node.x == null) return;

    if (view3D) {
      const distance = 200;
      const distRatio = 1 + distance / Math.hypot(node.x, node.y ?? 0, node.z ?? 0);
      fgRef.current.cameraPosition(
        { x: node.x * distRatio, y: (node.y ?? 0) * distRatio, z: (node.z ?? 0) * distRatio },
        { x: node.x, y: node.y ?? 0, z: node.z ?? 0 },
        1000,
      );
    } else {
      fgRef.current.centerAt(node.x, node.y ?? 0, 400);
      fgRef.current.zoom(3, 400);
    }
  }, [selectedNode, filteredData, view3D]);

  // Tuning fisica
  useEffect(() => {
    if (fgRef.current) {
      fgRef.current.d3Force("charge")?.strength(view3D ? -60 : -120).distanceMax(view3D ? 300 : 400);
      fgRef.current.d3Force("link")?.distance(view3D ? 50 : 80);
      fgRef.current.d3ReheatSimulation?.();
    }
  }, [filteredData, view3D]);

  const isHighlighted = useCallback(
    (nodeId: string) => {
      if (!neighborSet) return true; // nessuna selezione = tutti visibili
      return neighborSet.has(nodeId);
    },
    [neighborSet],
  );

  // ── 2D canvas paint ──
  const paintNode2D = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const label: string = node.name || node.id;
      const size = NODE_SIZES[node.label] || DEFAULT_SIZE;
      const color = NODE_COLORS[node.label] || DEFAULT_COLOR;
      const isSelected = selectedNode === node.id;
      const highlighted = isHighlighted(node.id);

      ctx.globalAlpha = highlighted ? 1.0 : 0.08;

      ctx.beginPath();
      ctx.arc(node.x, node.y, size, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();

      if (isSelected) {
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      if ((globalScale > 1.2 || isSelected) && highlighted) {
        const fontSize = Math.max(12 / globalScale, 3);
        ctx.font = `${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = "rgba(255,255,255,0.85)";
        const maxLen = Math.floor(24 / Math.max(1, 2.5 - globalScale));
        const truncated = label.length > maxLen ? label.slice(0, maxLen) + "..." : label;
        ctx.fillText(truncated, node.x, node.y + size + 1);
      }

      ctx.globalAlpha = 1.0;
    },
    [selectedNode, isHighlighted],
  );

  // ── 3D node objects ──
  const nodeThreeObject = useCallback(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (node: any) => {
      const size = NODE_SIZES[node.label] || 3;
      const color = NODE_COLORS[node.label] || DEFAULT_COLOR;
      const isSelected = selectedNode === node.id;
      const highlighted = isHighlighted(node.id);

      const geometry = new THREE.SphereGeometry(size, 16, 16);
      const material = new THREE.MeshPhongMaterial({
        color,
        transparent: true,
        opacity: highlighted ? (isSelected ? 1.0 : 0.85) : 0.06,
        emissive: new THREE.Color(isSelected ? color : "#000000"),
        emissiveIntensity: isSelected ? 0.5 : 0,
      });
      const mesh = new THREE.Mesh(geometry, material);

      if ((size >= 5 || isSelected) && highlighted) {
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        if (ctx) {
          const label: string = (node.name || node.id).slice(0, 24);
          canvas.width = 256;
          canvas.height = 64;
          ctx.font = "bold 20px sans-serif";
          ctx.fillStyle = "rgba(255,255,255,0.9)";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(label, 128, 32);

          const texture = new THREE.CanvasTexture(canvas);
          const spriteMaterial = new THREE.SpriteMaterial({ map: texture, transparent: true });
          const sprite = new THREE.Sprite(spriteMaterial);
          sprite.scale.set(30, 8, 1);
          sprite.position.set(0, size + 5, 0);
          mesh.add(sprite);
        }
      }

      return mesh;
    },
    [selectedNode, isHighlighted],
  );

  // ── Archi: evidenzia solo il vicinato del nodo selezionato ──
  const linkColorFn = useCallback(
    (link: GraphLink) => {
      if (!neighborSet) return "rgba(255,255,255,0.35)";
      const { src, tgt } = getLinkId(link);
      return neighborSet.has(src) && neighborSet.has(tgt)
        ? "rgba(255,255,255,0.7)"
        : "rgba(255,255,255,0.03)";
    },
    [neighborSet],
  );

  const linkWidthFn = useCallback(
    (link: GraphLink) => {
      if (!neighborSet) return 1;
      const { src, tgt } = getLinkId(link);
      return neighborSet.has(src) && neighborSet.has(tgt) ? 2 : 0.2;
    },
    [neighborSet],
  );

  if (!graphData) return null;

  const sharedProps = {
    ref: fgRef,
    graphData: filteredData,
    width,
    height,
    onNodeClick: (node: GraphNode) => onNodeClick?.(node),
    onBackgroundClick: () => onNodeClick?.(null),
    linkColor: linkColorFn,
    linkWidth: linkWidthFn,
    linkDirectionalArrowLength: 3,
    linkDirectionalArrowRelPos: 1,
    warmupTicks: 60,
    cooldownTicks: 150,
    cooldownTime: 3000,
    d3AlphaDecay: 0.08,
    d3VelocityDecay: 0.5,
    enableNodeDrag: true,
  };

  return (
    <div className="relative">
      {settling && (
        <div className="absolute inset-0 z-10 flex items-center justify-center rounded-lg bg-background/60 backdrop-blur-sm">
          <div className="flex flex-col items-center gap-2">
            <Loader2 className="size-8 animate-spin text-indigo-500" />
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
              Costruzione grafo ({filteredData.nodes.length} nodi, {filteredData.links.length} archi)…
            </p>
          </div>
        </div>
      )}
      {view3D ? (
        <ForceGraph3D
          {...sharedProps}
          nodeThreeObject={nodeThreeObject}
          nodeThreeObjectExtend={false}
          linkOpacity={0.6}
          backgroundColor="#09090b"
          enableNavigationControls
          showNavInfo={false}
        />
      ) : (
        <ForceGraph2D
          {...sharedProps}
          nodeCanvasObject={paintNode2D}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          nodePointerAreaPaint={(node: any, color: string, ctx: CanvasRenderingContext2D) => {
            const size = NODE_SIZES[node.label] || DEFAULT_SIZE;
            ctx.beginPath();
            ctx.arc(node.x, node.y, size + 2, 0, 2 * Math.PI);
            ctx.fillStyle = color;
            ctx.fill();
          }}
          backgroundColor="#09090b"
          enableZoomInteraction
          enablePanInteraction
          autoPauseRedraw
          minZoom={0.3}
          maxZoom={12}
        />
      )}
    </div>
  );
}

export { NODE_COLORS, NODE_SIZES };
