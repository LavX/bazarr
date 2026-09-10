export interface ProviderEndpoint {
  tag: string;
  name: string;
  available: boolean;
  inputPrice: number | null;
  outputPrice: number | null;
  throughput: number | null;
}

export const PROVIDER_SLUG =
  /^(?=.{1,160}$)[a-zA-Z0-9][a-zA-Z0-9._-]*(?:\/[a-zA-Z0-9][a-zA-Z0-9._-]*)*$/;

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function number(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function parseProviderEndpoints(payload: unknown): ProviderEndpoint[] {
  const endpoints = record(record(payload).data).endpoints;
  if (!Array.isArray(endpoints)) throw new Error("Invalid endpoint metadata");
  const parsed = new Map<string, ProviderEndpoint>();
  for (const value of endpoints) {
    const endpoint = record(value);
    const tag = endpoint.tag;
    if (typeof tag !== "string" || !PROVIDER_SLUG.test(tag)) continue;
    const pricing = record(endpoint.pricing);
    parsed.set(tag, {
      tag,
      name:
        typeof endpoint.provider_name === "string"
          ? endpoint.provider_name
          : tag,
      available: endpoint.status === 0,
      inputPrice: number(pricing.prompt),
      outputPrice: number(pricing.completion),
      // OpenRouter reports this as a plain number of tokens per second, alongside
      // latency_last_30m and uptime_last_30m, and sends null when it has no recent
      // measurement. The object form is what the request parameter takes, not what
      // the response carries, so it is only read as a fallback.
      throughput:
        number(endpoint.throughput_last_30m) ??
        number(record(endpoint.throughput_last_30m).p50),
    });
  }
  return [...parsed.values()];
}

export function priceLabel(price: number | null): string {
  if (price === null || !Number.isFinite(price * 1_000_000)) return "Unknown";
  if (price === 0) return "Free";
  return `$${(price * 1_000_000).toLocaleString("en-US", { maximumSignificantDigits: 5 })}/M`;
}
