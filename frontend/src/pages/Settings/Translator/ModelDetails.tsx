import { FunctionComponent } from "react";
import { Badge, Box, Group, SimpleGrid, Text } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";

// Average total token usage per translation (calibrated from real data)
const AVG_EPISODE_TOKENS = 90_000;
const AVG_MOVIE_TOKENS = 180_000;
const INPUT_RATIO = 0.4;
const OUTPUT_RATIO = 0.6;

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

const ModelDetailsCard: FunctionComponent<{ modelId: string }> = ({
  modelId,
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

  const costPerEpisodeSrt =
    AVG_EPISODE_TOKENS * INPUT_RATIO * promptCost +
    AVG_EPISODE_TOKENS * OUTPUT_RATIO * completionCost;
  const costPerMovieSrt =
    AVG_MOVIE_TOKENS * INPUT_RATIO * promptCost +
    AVG_MOVIE_TOKENS * OUTPUT_RATIO * completionCost;

  const formatCost = (cost: number) => {
    if (cost === 0) return "Free";
    if (cost < 0.001) return `$${(cost * 1000).toFixed(4)}/1K`;
    return `$${cost.toFixed(4)}`;
  };

  const formatPerMillion = (perToken: number) => {
    if (perToken === 0) return "Free";
    return `$${(perToken * 1_000_000).toFixed(2)}/M`;
  };

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
      <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs">
        <Box>
          <Text size="xs" c="dimmed">Input</Text>
          <Text size="sm" fw={600} c="gray.1">{formatPerMillion(promptCost)}</Text>
        </Box>
        <Box>
          <Text size="xs" c="dimmed">Output</Text>
          <Text size="sm" fw={600} c="gray.1">{formatPerMillion(completionCost)}</Text>
        </Box>
        <Box>
          <Text size="xs" c="dimmed">Est. / Movie</Text>
          <Text size="sm" fw={600} c="green.4">{formatCost(costPerMovieSrt)}</Text>
        </Box>
        <Box>
          <Text size="xs" c="dimmed">Est. / Episode</Text>
          <Text size="sm" fw={600} c="green.4">{formatCost(costPerEpisodeSrt)}</Text>
        </Box>
      </SimpleGrid>
      {hasCache && (
        <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="xs" mt="xs">
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
      )}
      <Group gap="xs" mt={6}>
        <Badge size="xs" variant="light" color="gray">
          {(model.context_length / 1024).toFixed(0)}K ctx
        </Badge>
        {model.top_provider?.max_completion_tokens && (
          <Badge size="xs" variant="light" color="gray">
            {(model.top_provider.max_completion_tokens / 1024).toFixed(0)}K max out
          </Badge>
        )}
        {model.supported_parameters?.includes("reasoning") && (
          <Badge size="xs" variant="light" color="blue">reasoning</Badge>
        )}
        {model.supported_parameters?.includes("temperature") && (
          <Badge size="xs" variant="light" color="gray">temperature</Badge>
        )}
        {hasCache && (
          <Badge size="xs" variant="light" color="cyan">caching</Badge>
        )}
      </Group>
    </Box>
  );
};

export default ModelDetailsCard;
