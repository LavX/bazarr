import { useEffect, useMemo, useState } from "react";
import {
  Button,
  Group,
  LoadingOverlay,
  Modal,
  Radio,
  ScrollArea,
  Stack,
  Table,
  Text,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import { usePromoteSyncSubtitle } from "@/apis/hooks";
import api from "@/apis/raw";
import { notification } from "@/modules/task";
import { formatTimestamp } from "@/pages/SubtitleEditor/CueTable";
import { getParser } from "@/pages/SubtitleEditor/parsers";
import type { SubtitleFormat } from "@/pages/SubtitleEditor/types";
import {
  buildSubtitleLanguageKey,
  getSyncEngineLabel,
  sortSyncOutputSubtitles,
} from "@/utilities/subtitles";

interface Props {
  opened: boolean;
  onClose: () => void;
  mediaType: "episode" | "movie";
  mediaId: number;
  original: Subtitle;
  outputs: Subtitle[];
}

interface Variant {
  key: string;
  label: string;
  subtitle: Subtitle;
  selectable: boolean;
}

interface CuePreview {
  time: string;
  text: string;
}

interface LoadedVariant extends Variant {
  cues: CuePreview[];
  error?: string;
}

function previewRows(content: string, format: string): CuePreview[] {
  try {
    const parser = getParser(format as SubtitleFormat);
    const result = parser.parse(content);
    if (result.cues.length > 0) {
      return result.cues.slice(0, 80).map((cue) => ({
        time: formatTimestamp(cue.startMs),
        text: cue.text,
      }));
    }
  } catch {
    // Fall back to raw blocks when the file is malformed or the parser cannot handle it.
  }

  return content
    .split(/\r?\n\r?\n/)
    .filter(Boolean)
    .slice(0, 80)
    .map((block, index) => ({
      time: `#${index + 1}`,
      text: block,
    }));
}

export default function SyncOutputCompareModal({
  opened,
  onClose,
  mediaType,
  mediaId,
  original,
  outputs,
}: Props) {
  const promote = usePromoteSyncSubtitle();
  const variants = useMemo<Variant[]>(() => {
    const sortedOutputs = sortSyncOutputSubtitles(outputs).slice(0, 3);
    return [
      {
        key: buildSubtitleLanguageKey(original),
        label: "Original",
        subtitle: original,
        selectable: false,
      },
      ...sortedOutputs.map((subtitle) => ({
        key: buildSubtitleLanguageKey(subtitle),
        label: getSyncEngineLabel(subtitle.modifier),
        subtitle,
        selectable: true,
      })),
    ];
  }, [original, outputs]);

  const [selected, setSelected] = useState("");
  const [loaded, setLoaded] = useState<LoadedVariant[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const firstOutput = variants.find((variant) => variant.selectable);
    setSelected(firstOutput?.key ?? "");
  }, [variants]);

  useEffect(() => {
    if (!opened) {
      return;
    }

    let cancelled = false;
    setLoading(true);

    Promise.all(
      variants.map(async (variant) => {
        try {
          const response = await api.subtitles.getContent(
            mediaType,
            mediaId,
            variant.key,
          );
          return {
            ...variant,
            cues: previewRows(
              response.data.content,
              response.data.format || "srt",
            ),
          };
        } catch (error) {
          const message =
            error instanceof Error ? error.message : "Failed to load subtitle";
          return { ...variant, cues: [], error: message };
        }
      }),
    ).then((items) => {
      if (!cancelled) {
        setLoaded(items);
        setLoading(false);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [mediaId, mediaType, opened, variants]);

  const maxRows =
    loaded.length > 0
      ? Math.max(1, ...loaded.map((variant) => variant.cues.length))
      : 0;
  const selectedVariant = variants.find((variant) => variant.key === selected);

  async function overwriteOriginal() {
    if (!selectedVariant) {
      return;
    }

    await promote.mutateAsync({
      mediaType,
      mediaId,
      targetLanguage: buildSubtitleLanguageKey(original),
      sourceLanguage: selectedVariant.key,
    });

    showNotification(
      notification.info(
        "Subtitle overwritten",
        `${selectedVariant.label} result copied to the original subtitle`,
      ),
    );
    onClose();
  }

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Compare Sync Outputs"
      size="95%"
      centered
    >
      <Stack pos="relative">
        <LoadingOverlay visible={loading} />
        <Radio.Group value={selected} onChange={setSelected}>
          <ScrollArea h="65vh">
            <Table
              striped
              stickyHeader
              withColumnBorders
              style={{ minWidth: variants.length * 280 }}
            >
              <Table.Thead>
                <Table.Tr>
                  {variants.map((variant) => (
                    <Table.Th key={variant.key} style={{ width: "25%" }}>
                      {variant.selectable ? (
                        <Radio value={variant.key} label={variant.label} />
                      ) : (
                        <Text fw={600}>{variant.label}</Text>
                      )}
                    </Table.Th>
                  ))}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {Array.from({ length: maxRows }).map((_, rowIndex) => (
                  <Table.Tr key={rowIndex}>
                    {loaded.map((variant) => {
                      const cue = variant.cues[rowIndex];
                      return (
                        <Table.Td
                          key={variant.key}
                          style={{ verticalAlign: "top", minWidth: "16rem" }}
                        >
                          {variant.error ? (
                            <Text c="red" size="xs">
                              {variant.error}
                            </Text>
                          ) : cue ? (
                            <>
                              <Text c="dimmed" ff="monospace" size="xs">
                                {cue.time}
                              </Text>
                              <Text
                                size="sm"
                                style={{ whiteSpace: "pre-wrap" }}
                              >
                                {cue.text}
                              </Text>
                            </>
                          ) : (
                            <Text c="dimmed" size="xs">
                              -
                            </Text>
                          )}
                        </Table.Td>
                      );
                    })}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Radio.Group>

        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={overwriteOriginal}
            disabled={!selectedVariant}
            loading={promote.isPending}
          >
            Overwrite Original
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
