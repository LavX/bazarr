import { FunctionComponent } from "react";
import { Badge, Group, Tooltip } from "@mantine/core";

interface HistoryProviderProps {
  provider?: string | null;
  aiTranslated?: boolean;
  action?: number;
}

const HistoryProvider: FunctionComponent<HistoryProviderProps> = ({
  provider,
  aiTranslated,
  action,
}) => (
  <Group gap={5} wrap="nowrap">
    <span>{provider}</span>
    {aiTranslated === true && action !== 6 && (
      <Tooltip label="AI-translated">
        <Badge
          size="xs"
          variant="light"
          color="violet"
          aria-label="AI-translated"
        >
          AI
        </Badge>
      </Tooltip>
    )}
  </Group>
);

export default HistoryProvider;
