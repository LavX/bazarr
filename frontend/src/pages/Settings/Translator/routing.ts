import { SelectorOption } from "@/components/inputs/Selector";

// Which OpenRouter provider serves the model. Mirrors the backend validator for
// translator.openrouter_provider_routing; the sidecar turns nitro and floor into
// OpenRouter slug shortcuts; SmartFast and Custom are sent through the translator API.
export const aiTranslatorProviderRoutingOptions: SelectorOption<string>[] = [
  { label: "SmartFast (speed + price, Default)", value: "smartfast" },
  { label: "Custom (selected providers)", value: "custom" },
  { label: "Fastest", value: "throughput" },
  { label: "Fastest + priority tier (:nitro)", value: "nitro" },
  { label: "Cheapest", value: "price" },
  { label: "Cheapest + flex tier (:floor)", value: "floor" },
  { label: "Lowest latency", value: "latency" },
  { label: "OpenRouter default (load balanced)", value: "default" },
];

// Routing shortcuts that can be appended to a model id. Every other
// variant, :free, :thinking, :online, :extended, :exacto and :batch among them,
// names a different model rather than a way to route to the same one.
const ROUTING_SUFFIXES = ["nitro", "floor", "smartfast"] as const;

export type RoutingSuffix = (typeof ROUTING_SUFFIXES)[number];

export interface SplitModelId {
  modelId: string;
  routing: RoutingSuffix | null;
}

// Separates the routing shortcut typed into a model id from the model itself, so
// the id can be looked up on OpenRouter and the routing can be shown in the
// selector that owns it.
//
// Stacked shortcuts are all removed and the last one typed wins. Leaving one on
// the id would contradict the selector, because both the Bazarr backend and the
// sidecar read a shortcut off the model id and let it beat the configured sort.
export function splitRoutingSuffix(rawModelId: string): SplitModelId {
  const trimmed = (rawModelId ?? "").trim();
  const colon = trimmed.indexOf(":");
  if (colon <= 0) {
    return { modelId: trimmed, routing: null };
  }

  // Every position is examined, not just the tail. A shortcut sitting in front of a
  // genuine variant, as in model:nitro:free, is still a shortcut, and the Bazarr
  // backend removes it from anywhere; stopping at the first non-routing tail here
  // left the two disagreeing about which model the request is even for.
  const asSuffix = (part: string) =>
    ROUTING_SUFFIXES.find((suffix) => suffix === part.toLowerCase()) ?? null;
  const variants = trimmed.slice(colon + 1).split(":");
  const shortcuts = variants.map(asSuffix).filter((s) => s !== null);

  return {
    modelId: [
      trimmed.slice(0, colon),
      ...variants.filter((p) => !asSuffix(p)),
    ].join(":"),
    routing: shortcuts.length ? shortcuts[shortcuts.length - 1] : null,
  };
}

export function routingLabel(value: string): string {
  return (
    aiTranslatorProviderRoutingOptions.find((o) => o.value === value)?.label ??
    value
  );
}
