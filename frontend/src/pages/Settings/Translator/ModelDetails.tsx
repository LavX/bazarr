import { FunctionComponent } from "react";
import { Badge, Box, Group, SimpleGrid, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";

// Average total token usage per translation (calibrated from real data)
// Episode ~90K tokens, Movie ~180K tokens
// With caching: ~70% of input tokens hit cache on subsequent episodes (same series = same system prompt)
const AVG_EPISODE_TOKENS = 90_000;
const AVG_MOVIE_TOKENS = 180_000;
const INPUT_RATIO = 0.4;
const OUTPUT_RATIO = 0.6;
const CACHE_HIT_RATIO = 0.7; // 70% of input tokens from cache on repeat runs

// Reasoning adds extra output tokens (thinking). Multiplier on output token count.
const REASONING_OUTPUT_MULTIPLIER: Record<string, number> = {
  disabled: 1,
  low: 1.3,
  medium: 1.8,
  high: 2.5,
};

interface OpenRouterModel {
  id: string;
  name: string;
  context_length: number;
  pricing: {
    prompt: string;
    completion: string;
    cache_read?: string;
    cache_write?: string;
  };
  architecture?: {
    modality: string;
  };
  top_provider?: {
    max_completion_tokens?: number;
  };
  supported_parameters?: string[];
}

function useOpenRouterModelDetails(modelId: string) {
  return useQuery({
    queryKey: ["openrouter", "models", modelId],
    queryFn: async () => {
      const response = await fetch("https://openrouter.ai/api/v1/models");
      const data = await response.json();
      const found = data.data?.find(
        (m: OpenRouterModel) => m.id === modelId,
      );
      return (found as OpenRouterModel) || null;
    },
    enabled: !!modelId,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
}

function calcCost(
  totalTokens: number,
  promptCost: number,
  completionCost: number,
  cacheReadCost: number | null,
  reasoningMultiplier: number,
) {
  const inputTokens = totalTokens * INPUT_RATIO;
  const outputTokens = totalTokens * OUTPUT_RATIO * reasoningMultiplier;

  // Standard cost (no caching)
  const standard = inputTokens * promptCost + outputTokens * completionCost;

  // Cached cost (subsequent episodes reuse system prompt)
  if (cacheReadCost !== null) {
    const cachedInput = inputTokens * CACHE_HIT_RATIO * cacheReadCost;
    const uncachedInput = inputTokens * (1 - CACHE_HIT_RATIO) * promptCost;
    const cached = cachedInput + uncachedInput + outputTokens * completionCost;
    return { standard, cached };
  }

  return { standard, cached: null };
}

const formatCost = (cost: number) => {
  if (cost === 0) return "Free";
  if (cost < 0.001) return `$${(cost * 1000).toFixed(4)}/1K`;
  return `$${cost.toFixed(4)}`;
};

const formatPerMillion = (perToken: number) => {
  if (perToken === 0) return "Free";
  return `$${(perToken * 1_000_000).toFixed(2)}/M`;
};

interface ModelDetailsProps {
  modelId: string;
  reasoningLevel?: string;
}

const ModelDetailsCard: FunctionComponent<ModelDetailsProps> = ({
  modelId,
  reasoningLevel = "disabled",
}) => {
  const { data: model, isLoading: loading } = useOpenRouterModelDetails(modelId);

  if (loading) {
    return (
      <Text size="xs" c="dimmed" mt="xs">
        Loading model details...
      </Text>
    );
  }

  if (!model) {
    return (
      <Text size="xs" c="dimmed" mt="xs">
        Model details unavailable for {modelId}
      </Text>
    );
  }

  const promptCost = parseFloat(model.pricing.prompt);
  const completionCost = parseFloat(model.pricing.completion);
  const cacheReadCost = model.pricing.cache_read
    ? parseFloat(model.pricing.cache_read)
    : null;
  const cacheWriteCost = model.pricing.cache_write
    ? parseFloat(model.pricing.cache_write)
    : null;
  const hasCache = cacheReadCost !== null || cacheWriteCost !== null;

  const supportsReasoning = model.supported_parameters?.includes("reasoning") ?? false;
  const effectiveReasoning = supportsReasoning ? reasoningLevel : "disabled";
  const reasoningMul = REASONING_OUTPUT_MULTIPLIER[effectiveReasoning] ?? 1;

  const episode = calcCost(AVG_EPISODE_TOKENS, promptCost, completionCost, cacheReadCost, reasoningMul);
  const movie = calcCost(AVG_MOVIE_TOKENS, promptCost, completionCost, cacheReadCost, reasoningMul);

  return (
    <Box mt="xs">
      <Text
        size="xs"
        c="dimmed"
        tt="uppercase"
        style={{ letterSpacing: 0.5 }}
        fw={600}
        mb="xs"
      >
        {model.name}
      </Text>
      {/* Per-million pricing — always show all available prices */}
      <SimpleGrid cols={{ base: 2, sm: hasCache ? 4 : 2 }} spacing="xs">
        <Box>
          <Text size="xs" c="dimmed">Input</Text>
          <Text size="sm" fw={600}>{formatPerMillion(promptCost)}</Text>
        </Box>
        <Box>
          <Text size="xs" c="dimmed">Output</Text>
          <Text size="sm" fw={600}>{formatPerMillion(completionCost)}</Text>
        </Box>
        {cacheReadCost !== null && (
          <Box>
            <Text size="xs" c="dimmed">Cache Read</Text>
            <Text size="sm" fw={600} c="cyan.4">{formatPerMillion(cacheReadCost)}</Text>
          </Box>
        )}
        {cacheWriteCost !== null && (
          <Box>
            <Text size="xs" c="dimmed">Cache Write</Text>
            <Text size="sm" fw={600} c="cyan.4">{formatPerMillion(cacheWriteCost)}</Text>
          </Box>
        )}
      </SimpleGrid>
      {/* Estimations — single best estimate per type */}
      <SimpleGrid cols={{ base: 2, sm: 2 }} spacing="xs" mt="xs">
        <Box>
          <Text size="xs" c="dimmed">Est. / Episode</Text>
          <Text size="sm" fw={600} c="green.4">
            {formatCost(episode.cached ?? episode.standard)}
          </Text>
        </Box>
        <Box>
          <Text size="xs" c="dimmed">Est. / Movie</Text>
          <Text size="sm" fw={600} c="green.4">
            {formatCost(movie.cached ?? movie.standard)}
          </Text>
        </Box>
      </SimpleGrid>
      <Group gap="xs" mt={6}>
        <Badge size="xs" variant="light" color="gray">
          Context: {(model.context_length / 1024).toFixed(0)}K tokens
        </Badge>
        {model.top_provider?.max_completion_tokens && (
          <Badge size="xs" variant="light" color="gray">
            Max output: {(model.top_provider.max_completion_tokens / 1024).toFixed(0)}K tokens
          </Badge>
        )}
        {model.supported_parameters?.includes("reasoning") && (
          <Badge size="xs" variant="light" color="blue">Reasoning</Badge>
        )}
        {hasCache && (
          <Badge size="xs" variant="light" color="cyan">Prompt caching</Badge>
        )}
      </Group>
    </Box>
  );
};

export { useOpenRouterModelDetails };
export default ModelDetailsCard;
