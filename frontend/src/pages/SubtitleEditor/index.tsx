import { useMemo } from "react";
import { useParams } from "react-router";
import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Center,
  Group,
  Loader,
  Stack,
  Text,
} from "@mantine/core";
import { useSubtitleContent } from "@/apis/hooks/subtitles";
import CueTable from "./CueTable";
import { getParser } from "./parsers";
import type { ParseResult,SubtitleFormat } from "./types";

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

export default function SubtitleEditor() {
  const { mediaType, mediaId, language } = useParams();

  const { data, isLoading, error } = useSubtitleContent(
    mediaType,
    mediaId ? Number(mediaId) : undefined,
    language,
  );

  const parseResult = useMemo<ParseResult | null>(() => {
    if (!data) return null;
    try {
      return getParser(data.format as SubtitleFormat).parse(data.content);
    } catch {
      return null;
    }
  }, [data]);

  if (isLoading) {
    return (
      <Center style={{ height: "100%" }}>
        <Loader />
      </Center>
    );
  }

  if (error) {
    return (
      <Alert color="red" title="Error" m="md">
        {error.message || "Failed to load subtitle content"}
      </Alert>
    );
  }

  if (data && !parseResult) {
    return (
      <Alert color="red" title="Parse Error" m="md">
        Failed to parse subtitle file
      </Alert>
    );
  }

  if (!data || !parseResult) {
    return null;
  }

  const isSeries = mediaType === "episode" || mediaType === "series";
  const listPath = isSeries ? "/series" : "/movies";
  const listLabel = isSeries ? "Series" : "Movies";
  const detailPath = data.mediaId
    ? isSeries
      ? `/series/${data.mediaId}`
      : `/movies/${data.mediaId}`
    : undefined;

  return (
    <Stack gap="sm" style={{ height: "100%", padding: "0" }}>
      <Group justify="space-between" px="md" pt="xs">
        <Breadcrumbs>
          <Anchor href={listPath}>{listLabel}</Anchor>
          {data.mediaTitle && detailPath && (
            <Anchor href={detailPath}>{data.mediaTitle}</Anchor>
          )}
          <Text>Subtitle Viewer</Text>
        </Breadcrumbs>
        <Group gap="xs">
          <Badge variant="light" size="sm">
            {data.format.toUpperCase()}
          </Badge>
          {data.language && (
            <Badge variant="light" color="teal" size="sm">
              {data.language}
            </Badge>
          )}
          <Badge variant="outline" color="gray" size="sm">
            {data.encoding}
          </Badge>
          <Text size="xs" c="var(--bz-text-tertiary)">
            {parseResult.cues.length} cues
          </Text>
          <Text size="xs" c="var(--bz-text-tertiary)">
            {formatFileSize(data.size)}
          </Text>
        </Group>
      </Group>
      <div style={{ flex: 1, minHeight: 0, display: "flex", padding: "0 var(--mantine-spacing-md) var(--mantine-spacing-md)" }}>
        <CueTable cues={parseResult.cues} />
      </div>
    </Stack>
  );
}
