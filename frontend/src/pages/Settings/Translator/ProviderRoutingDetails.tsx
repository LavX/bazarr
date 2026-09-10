import { useState } from "react";
import {
  Alert,
  Button,
  Group,
  Select,
  Stack,
  TagsInput,
  Text,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import {
  parseProviderEndpoints,
  priceLabel,
  PROVIDER_SLUG,
} from "./providerEndpoints";
import { splitRoutingSuffix } from "./routing";

const ORDER_KEY = "settings-translator-openrouter_provider_order";

function CustomProviders() {
  const model = useSettingValue<string>("settings-translator-openrouter_model");
  const order = useSettingValue<string[]>(ORDER_KEY) ?? [];
  const { setValue } = useFormActions();
  const { modelId } = splitRoutingSuffix(model ?? "");
  const catalog = useQuery({
    queryKey: ["openrouter", "endpoints", modelId],
    queryFn: async ({ signal }) => {
      const path = modelId.split("/").map(encodeURIComponent).join("/");
      const response = await fetch(
        `https://openrouter.ai/api/v1/models/${path}/endpoints`,
        {
          signal,
          credentials: "omit",
        },
      );
      if (!response.ok) throw new Error("Provider metadata unavailable");
      return parseProviderEndpoints(await response.json());
    },
    enabled: !!modelId,
    staleTime: 60_000,
    retry: false,
  });
  const [entryError, setEntryError] = useState<string | null>(null);
  const update = (values: string[]) => {
    const normalized = [
      ...new Set(
        values.map((value) => value.trim().toLowerCase()).filter(Boolean),
      ),
    ];
    if (
      normalized.length > 20 ||
      normalized.some((slug) => !PROVIDER_SLUG.test(slug))
    ) {
      setEntryError(
        "Enter a valid provider slug, up to 160 characters, with no empty slash segments.",
      );
      return;
    }
    setEntryError(null);
    setValue(normalized, ORDER_KEY);
  };
  const move = (index: number, delta: number) => {
    const next = [...order];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    update(next);
  };
  const invalid = order.some((slug) => !PROVIDER_SLUG.test(slug));
  const error = !order.length
    ? "Choose at least one provider before translating."
    : invalid || order.length > 20
      ? "Use up to 20 provider slugs containing letters, numbers, dots, underscores, slashes or hyphens."
      : undefined;

  return (
    <Stack gap="xs">
      <Text size="sm">
        AI Subtitle Translator will try these providers in order. Providers
        outside this list are excluded. If none can serve the model, translation
        fails.
      </Text>
      <Select
        label="Add a provider for this model"
        placeholder={
          catalog.isLoading
            ? "Loading providers..."
            : "Search available endpoints"
        }
        searchable
        value={null}
        disabled={!modelId || order.length >= 20}
        data={(catalog.data ?? [])
          .filter((endpoint) => !order.includes(endpoint.tag))
          .map((endpoint) => ({
            value: endpoint.tag,
            label: `${endpoint.name} (${endpoint.tag}) | Input ${priceLabel(endpoint.inputPrice)}, output ${priceLabel(endpoint.outputPrice)}${endpoint.throughput === null ? "" : ` | ${endpoint.throughput.toFixed(0)} tokens/s`}${endpoint.available ? "" : " | Unavailable"}`,
            disabled: !endpoint.available,
          }))}
        onChange={(value) => {
          if (value) update([...order, value]);
        }}
        nothingFoundMessage="No available endpoints. You can enter a provider slug below."
      />
      {catalog.isError && (
        <Text size="xs" c="orange">
          Provider metadata is unavailable. Your selection is preserved; you can
          enter provider slugs below.
        </Text>
      )}
      {catalog.data?.length === 0 && (
        <Text size="xs">
          No endpoints were listed for this model. You can enter provider slugs
          below.
        </Text>
      )}
      <TagsInput
        label="Selected providers (custom slugs)"
        description="Enter an OpenRouter slug such as deepinfra or an exact endpoint slug. Press Enter to add it."
        value={[...order]}
        onChange={update}
        maxTags={20}
        error={entryError ?? error}
        clearable
      />
      {order.map((slug, index) => {
        const endpoint = catalog.data?.find((item) => item.tag === slug);
        return (
          <Group key={slug} justify="space-between" wrap="wrap">
            <Text size="xs">
              {index + 1}. {slug}
              {endpoint
                ? ` | Input ${priceLabel(endpoint.inputPrice)}, output ${priceLabel(endpoint.outputPrice)}${endpoint.available ? "" : " | Unavailable"}`
                : " | Price and availability unverified"}
            </Text>
            <Group gap={4}>
              <Button
                size="compact-xs"
                variant="subtle"
                aria-label={`Move ${slug} up`}
                disabled={index === 0}
                onClick={() => move(index, -1)}
              >
                Up
              </Button>
              <Button
                size="compact-xs"
                variant="subtle"
                aria-label={`Move ${slug} down`}
                disabled={index === order.length - 1}
                onClick={() => move(index, 1)}
              >
                Down
              </Button>
            </Group>
          </Group>
        );
      })}
      <Text size="xs" c="dimmed">
        Quoted input/output prices are per million tokens. Extra fees and
        context pricing may apply. Custom routing uses your choices without
        SmartFast price filtering. Review the list when changing models.
      </Text>
    </Stack>
  );
}

export default function ProviderRoutingDetails() {
  const routing = useSettingValue<string>(
    "settings-translator-openrouter_provider_routing",
  );
  if (routing === "smartfast")
    return (
      <Alert color="blue" title="SmartFast routing">
        Bazarr asks AI Subtitle Translator to choose fast providers within its
        price limits and keep provider affinity within the translation session.
        Requires a translator build with SmartFast support (2.0.0 or later).
        Custom provider selections are not sent in this mode.
      </Alert>
    );
  return routing === "custom" ? <CustomProviders /> : null;
}
