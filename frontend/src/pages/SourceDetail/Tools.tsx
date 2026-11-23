import { useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Group,
  Loader,
  ScrollArea,
  Stack,
  Text,
} from "@mantine/core";
import { useQueries } from "@tanstack/react-query";
import { useMediaSources } from "@/apis/hooks/mediaSources";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import Language from "@/components/bazarr/Language";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { withModal } from "@/modules/modals";
import { basename } from "@/utilities/files";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  isCombinedOutputSubtitle,
} from "@/utilities/subtitles";
import {
  sourceDetailApi,
  sourceDetailKeys,
  sourceError,
  sourceItemHasFile,
  sourceOwnerAvailable,
  useRefreshSourceDetails,
  useSourceWrite,
} from "./api";
import { SourceHistoryButton, SourceUpgradeButton } from "./History";
import {
  OpenSourceEditor,
  SourceCombineButton,
  SourceSubtitleMenu,
  sourceSubtitleSelection,
} from "./SubtitleActions";

export interface SourceToolsTarget {
  kind: "show" | "movie" | "episode";
  id: number;
  source_id: number | null;
  library_id: number;
  path?: string | null;
}

async function readTarget(target: SourceToolsTarget, signal: AbortSignal) {
  const ownsItem = (item: { source_id: number; library_id: number }) =>
    item.source_id === target.source_id &&
    item.library_id === target.library_id;
  if (target.kind !== "show") {
    const item = await sourceDetailApi.item(target.id, signal, target.kind);
    if (!ownsItem(item) || (target.path && item.path !== target.path)) {
      throw new Error("The selected item moved or is no longer available.");
    }
    return [item];
  }
  const group = await sourceDetailApi.group(target.id, signal);
  if (!ownsItem(group))
    throw new Error("The selected show is no longer available.");
  const { data } = await sourceDetailApi.episodes(
    group.id,
    0,
    -1,
    signal,
    true,
  );
  return data.filter((item) => ownsItem(item) && item.group_id === group.id);
}

function SourceToolsView({
  targets,
  openEditor,
}: {
  targets: SourceToolsTarget[];
  openEditor: OpenSourceEditor;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const write = useSourceWrite({ notifyError: true });
  const refresh = useRefreshSourceDetails();
  const owners = useMediaSources();
  const queries = useQueries({
    queries: targets.map((target) => ({
      queryKey: [
        ...sourceDetailKeys.root,
        "tool-target",
        target.kind,
        target.id,
        target.source_id,
        target.library_id,
        target.path,
      ],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        withInlineErrors(() => readTarget(target, signal)),
      retry: false,
    })),
  });
  const items = queries
    .flatMap((query) => (query.isError ? [] : (query.data ?? [])))
    .filter(
      (item) =>
        sourceItemHasFile(item) &&
        owners.isEnabled &&
        sourceOwnerAvailable(owners.data?.data, item),
    );
  const withFiles = items.map((item) => ({
    item,
    files: item.subtitles
      .filter((subtitle) => subtitle.path)
      .map((subtitle) => ({
        subtitle,
        key: JSON.stringify([
          item.id,
          item.source_id,
          item.library_id,
          item.path,
          buildSubtitleLanguageKey(subtitle),
          subtitle.path,
        ]),
      })),
  }));
  const files = withFiles.flatMap(({ item, files }) =>
    files.map((file) => ({ item, ...file })),
  );
  const chosen = files.filter(({ key }) => selected.includes(key));
  const loading = queries.some((query) => query.isPending) || owners.isLoading;
  const error = queries.find((query) => query.isError)?.error;

  return (
    <Stack>
      {loading && <Loader aria-label="Loading source subtitles" />}
      {error && <Alert color="red">{sourceError(error)}</Alert>}
      {!loading && files.length === 0 && (
        <Text>No external subtitles on the selected items.</Text>
      )}
      {files.length > 0 && (
        <Group justify="space-between">
          <Checkbox
            label="Select all subtitles"
            checked={chosen.length === files.length}
            indeterminate={chosen.length > 0 && chosen.length < files.length}
            onChange={(event) =>
              setSelected(
                event.currentTarget.checked ? files.map(({ key }) => key) : [],
              )
            }
          />
          <SubtitleToolsMenu
            selections={chosen.map(({ item, subtitle }) =>
              sourceSubtitleSelection(item, subtitle),
            )}
            fileActions={false}
            canSync={chosen.every(({ subtitle }) =>
              canSynchronizeSubtitle(subtitle),
            )}
            canTranslate={chosen.every(
              ({ subtitle }) => !isCombinedOutputSubtitle(subtitle),
            )}
            onAction={(action) => {
              if (action !== "delete") return;
              write.mutate(async () => {
                const results = await Promise.allSettled(
                  chosen.map(async ({ item, subtitle }) => {
                    await sourceDetailApi.requireCurrentItem(item, subtitle);
                    return sourceDetailApi.removeSubtitle(item.id, subtitle);
                  }),
                );
                const failed = results.filter(
                  (result) => result.status === "rejected",
                ).length;
                if (failed > 0) {
                  await refresh();
                  throw new Error(
                    `${failed} of ${chosen.length} subtitles could not be deleted. Refresh the list and try again.`,
                  );
                }
              });
            }}
          >
            <Button
              disabled={chosen.length === 0 || write.isPending}
              variant="light"
            >
              Select Action
            </Button>
          </SubtitleToolsMenu>
        </Group>
      )}
      <ScrollArea.Autosize mah="55vh">
        <Stack>
          {withFiles
            .filter(({ files }) => files.length > 0)
            .map(({ item, files }) => (
              <Stack gap="xs" key={item.id}>
                <Group justify="space-between" align="start">
                  <Text fw={600}>{item.title}</Text>
                  <Group gap="xs">
                    <SourceHistoryButton item={item} />
                    <SourceUpgradeButton item={item} />
                    <SourceCombineButton item={item} />
                  </Group>
                </Group>
                {files.map(({ subtitle, key }) => (
                  <Group key={key} justify="space-between">
                    <Checkbox
                      checked={selected.includes(key)}
                      label={
                        <>
                          <Language.Text value={subtitle} />{" "}
                          {basename(subtitle.path ?? "")}
                        </>
                      }
                      onChange={(event) => {
                        const checked = event.currentTarget.checked;
                        setSelected((current) =>
                          checked
                            ? [...current, key]
                            : current.filter((value) => value !== key),
                        );
                      }}
                    />
                    <SourceSubtitleMenu
                      item={item}
                      subtitle={subtitle}
                      openEditor={openEditor}
                    />
                  </Group>
                ))}
              </Stack>
            ))}
        </Stack>
      </ScrollArea.Autosize>
    </Stack>
  );
}

export const SourceToolsModal = withModal(
  SourceToolsView,
  "source-subtitle-tools",
  {
    title: "Subtitle Tools",
    size: "xl",
  },
);
