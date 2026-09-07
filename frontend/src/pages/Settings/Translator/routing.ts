import { SelectorOption } from "@/components/inputs/Selector";

// Which OpenRouter provider serves the model. Mirrors the backend validator for
// translator.openrouter_provider_routing; the sidecar turns nitro and floor into
// the OpenRouter slug shortcuts and the rest into provider.sort.
export const aiTranslatorProviderRoutingOptions: SelectorOption<string>[] = [
  { label: "Fastest (Default)", value: "throughput" },
  { label: "Fastest + priority tier (:nitro)", value: "nitro" },
  { label: "Cheapest", value: "price" },
  { label: "Cheapest + flex tier (:floor)", value: "floor" },
  { label: "Lowest latency", value: "latency" },
  { label: "OpenRouter default (load balanced)", value: "default" },
];

// The two OpenRouter shortcuts that can be appended to a model id. Every other
// variant, :free, :thinking, :online, :extended, :exacto and :batch among them,
// names a different model rather than a way to route to the same one.
const ROUTING_SUFFIXES = ["nitro", "floor"] as const;

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
  let modelId = (rawModelId ?? "").trim();
  let routing: RoutingSuffix | null = null;

  for (;;) {
    const colon = modelId.lastIndexOf(":");
    if (colon <= 0) {
      return { modelId, routing };
    }

    const tail = modelId.slice(colon + 1).toLowerCase();
    const found = ROUTING_SUFFIXES.find((suffix) => suffix === tail);
    if (!found) {
      return { modelId, routing };
    }

    routing = routing ?? found;
    modelId = modelId.slice(0, colon);
  }
}

export function routingLabel(value: string): string {
  return (
    aiTranslatorProviderRoutingOptions.find((o) => o.value === value)?.label ??
    value
  );
}
