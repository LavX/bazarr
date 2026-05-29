import { FunctionComponent } from "react";
import { Badge, Group, Text } from "@mantine/core";

import { getCombinedLabel } from "@/utilities/subtitles";

interface Props {
  subtitle: Subtitle;
}

const CombinedSubtitleBadge: FunctionComponent<Props> = ({ subtitle }) => {
  const isAss = subtitle.path?.toLowerCase().endsWith(".ass") ?? false;
  const label = getCombinedLabel(subtitle);
  return (
    <Group gap="xs">
      <Badge color={isAss ? "violet" : "blue"} variant="light">
        Combined ({isAss ? "ASS" : "SRT"})
      </Badge>
      <Text size="sm">{label}</Text>
    </Group>
  );
};

export default CombinedSubtitleBadge;
