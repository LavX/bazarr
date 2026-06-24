// eslint-disable-next-line no-restricted-imports
import { dependencies } from "../package.json";

const vendors = ["react", "react-router", "react-dom"];

const ui = [
  "@mantine/core",
  "@mantine/hooks",
  "@mantine/form",
  "@mantine/modals",
  "@mantine/notifications",
  "@mantine/dropzone",
];

const query = [
  "@tanstack/react-query",
  "@tanstack/react-query-devtools",
  "@tanstack/react-table",
];

const charts = [
  "recharts",
  "d3-array",
  "d3-interpolate",
  "d3-scale",
  "d3-shape",
  "d3-time",
];

const utils = ["axios", "socket.io-client", "lodash", "clsx"];

// Build a package -> chunk-name lookup that mirrors the previous object-form
// `manualChunks` map. Named groups come first; every other production
// dependency gets its own chunk (one package per chunk), matching the prior
// behavior. Vite 8 / Rolldown only accepts the function form of manualChunks,
// not the object form, so this is expressed as a resolver.
function buildPackageChunkMap(): Record<string, string> {
  const named: Record<string, string[]> = {
    vendors,
    ui,
    query,
    charts,
    utils,
  };

  const map: Record<string, string> = {};
  for (const chunkName in named) {
    for (const pkg of named[chunkName]) {
      map[pkg] = chunkName;
    }
  }

  const excludeList = [...vendors, ...ui, ...query, ...charts, ...utils];
  for (const key in dependencies) {
    if (!excludeList.includes(key)) {
      map[key] = key;
    }
  }

  return map;
}

const packageChunkMap = buildPackageChunkMap();

// Match the package name out of a node_modules path, handling scoped packages.
function packageNameFromId(id: string): string | null {
  const marker = "node_modules/";
  const idx = id.lastIndexOf(marker);
  if (idx === -1) return null;

  const rest = id.slice(idx + marker.length);
  const parts = rest.split("/");
  if (parts.length === 0) return null;

  if (parts[0].startsWith("@") && parts.length > 1) {
    return `${parts[0]}/${parts[1]}`;
  }
  return parts[0];
}

function manualChunks(id: string): string | undefined {
  const normalized = id.replace(/\\/g, "/");
  if (!normalized.includes("node_modules/")) return undefined;

  const pkg = packageNameFromId(normalized);
  if (pkg && packageChunkMap[pkg]) {
    return packageChunkMap[pkg];
  }
  return undefined;
}

export default manualChunks;
