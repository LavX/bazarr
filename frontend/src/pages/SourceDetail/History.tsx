/* eslint-disable camelcase */
import { useState } from "react";
import { Alert, Badge, Center, Group, Stack, Text } from "@mantine/core";
import {
  faArrowUp,
  faFileExcel,
  faHistory,
} from "@fortawesome/free-solid-svg-icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ColumnDef } from "@tanstack/react-table";
import queryClient from "@/apis/queries";
import {
  expireSourceSubtitleDocument,
  sourceSubtitleVariant,
} from "@/apis/queries/sourceSubtitleDocument";
import BaseApi from "@/apis/raw/base";
import { withInlineErrors } from "@/apis/raw/inlineWrites";
import { Action, Toolbox } from "@/components";
import QueryOverlay from "@/components/async/QueryOverlay";
import { HistoryIcon } from "@/components/bazarr";
import HistoryProvider from "@/components/bazarr/HistoryProvider";
import Language from "@/components/bazarr/Language";
import StateIcon from "@/components/StateIcon";
import PageTable from "@/components/tables/PageTable";
import TextPopover from "@/components/TextPopover";
import { useModals, withModal } from "@/modules/modals";
import { awaitJobResult, QueuedSearch } from "@/utilities/jobs";
import {
  sameSourceItem,
  sourceDetailApi,
  sourceDetailKeys,
  sourceError,
  SourceItem,
  sourceItemHasFile,
  useRefreshSourceDetails,
  useSourceItem,
  useSourceOwnerAvailable,
} from "./api";

export interface SourceHistoryRow {
  origin: "source";
  source_key: MediaSourceKey;
  source_id: number | null;
  item_id: number | null;
  item_kind: SourceItem["kind"];
  history_id: number;
  action: number;
  language: Language.Info | null;
  provider: string | null;
  ai_translated?: boolean;
  score: string | null;
  matches: string[];
  dont_matches: string[];
  timestamp: string;
  parsed_timestamp: string;
  description: string | null;
  subtitles_path: string | null;
  video_path: string | null;
  subs_id: string | null;
  blacklisted: boolean;
  upgradable: boolean;
  upgradedFromId?: number | null;
}

function ownsHistory(row: SourceHistoryRow, item: SourceItem) {
  return (
    row.origin === "source" &&
    row.source_id === item.source_id &&
    row.item_id === item.id &&
    row.item_kind === item.kind &&
    row.video_path === item.path
  );
}

export function sourceReleaseForSubtitle(
  rows: SourceHistoryRow[] | undefined,
  item: SourceItem,
  subtitle: Subtitle,
) {
  // Source history is newest first. A replacement or deletion ends the old
  // release's provenance even when the new subtitle keeps the same path.
  const release = rows?.find(
    (row) =>
      ownsHistory(row, item) &&
      row.subtitles_path === subtitle.path &&
      ![7, 9].includes(row.action),
  );
  if (
    release &&
    [1, 2, 3].includes(release.action) &&
    !release.blacklisted &&
    release.provider &&
    release.subs_id &&
    release.subtitles_path &&
    release.language?.code2 === subtitle.code2 &&
    !!release.language.hi === !!subtitle.hi &&
    !!release.language.forced === !!subtitle.forced
  ) {
    return release;
  }
  return undefined;
}

class SourceHistoryApi extends BaseApi {
  constructor() {
    super("/sources/items");
  }

  history(id: number, signal?: AbortSignal) {
    return this.get<{ data: SourceHistoryRow[]; total: number }>(
      `/${id}/history`,
      { length: -1 },
      signal,
    );
  }

  async upgrade(id: number, historyId?: number) {
    const { data } = await this.post<QueuedSearch>(`/${id}/upgrade`, {
      history_id: historyId,
    });
    return data;
  }

  async exclude(id: number, row: SourceHistoryRow) {
    await this.post(`/${id}/blacklist`, {
      history_id: row.history_id,
      provider: row.provider,
      subs_id: row.subs_id,
      subtitles_path: row.subtitles_path,
      language: row.language?.code2,
      hi: !!row.language?.hi,
      forced: !!row.language?.forced,
    });
    if (row.language) {
      const { code2, hi, forced } = row.language;
      void expireSourceSubtitleDocument(
        queryClient,
        id,
        sourceSubtitleVariant(code2, hi, forced),
      );
    }
  }
}

export const sourceHistoryApi = new SourceHistoryApi();

export function useSourceHistory(item: SourceItem, enabled = true) {
  return useQuery({
    queryKey: [
      ...sourceDetailKeys.root,
      "history",
      item.source_id,
      item.library_id,
      item.kind,
      item.id,
    ],
    queryFn: ({ signal }) =>
      withInlineErrors(() => sourceHistoryApi.history(item.id, signal)),
    enabled:
      enabled &&
      Number.isSafeInteger(item.id) &&
      item.id > 0 &&
      item.kind !== "sports",
    retry: false,
    placeholderData: undefined,
  });
}

function useCurrentSourceItem(item: SourceItem) {
  const current = useSourceItem(
    item.id,
    item.kind === "episode" ? "episode" : "movie",
  );
  const available = useSourceOwnerAvailable(current.data ?? item);
  const value = current.data;
  const sameItem = value !== undefined && sameSourceItem(item, value);
  const allowed =
    sameItem &&
    !current.isError &&
    available &&
    !!value.path &&
    !value.server_missing &&
    !value.arr_shadow;
  return { current, item: value, allowed };
}

export function useSourceUpgrade(item: SourceItem, disabled = false) {
  const client = useQueryClient();
  const refresh = useRefreshSourceDetails();
  const available = useSourceOwnerAvailable(item);
  const [queued, setQueued] = useState<QueuedSearch>();
  const allowed =
    available &&
    sourceItemHasFile(item) &&
    item.kind !== "sports" &&
    ["identified", "parsed"].includes(item.identity_state) &&
    (item.effective_profile_id !== undefined
      ? item.effective_profile_id
      : item.profile_id) !== null &&
    !disabled;
  const canUpgrade = allowed && item.upgradable === true;
  const mutation = useMutation({
    mutationKey: [
      ...sourceDetailKeys.root,
      "upgrade",
      item.source_id,
      item.library_id,
      item.kind,
      item.id,
    ],
    mutationFn: async (row?: SourceHistoryRow) => {
      if (
        !allowed ||
        (row ? !ownsHistory(row, item) || !row.upgradable : !canUpgrade)
      ) {
        throw new Error("This subtitle is no longer available for an upgrade.");
      }
      setQueued(undefined);
      const live = await sourceDetailApi.requireCurrentItem(item);
      if (
        !["identified", "parsed"].includes(live.identity_state) ||
        (live.effective_profile_id !== undefined
          ? live.effective_profile_id
          : live.profile_id) === null ||
        !live.upgradable
      ) {
        throw new Error("This subtitle is no longer available for an upgrade.");
      }
      if (row) {
        const fresh = await withInlineErrors(() =>
          sourceHistoryApi.history(item.id),
        );
        if (
          !fresh.data.some(
            (value) =>
              value.history_id === row.history_id &&
              value.upgradable &&
              ownsHistory(value, live),
          )
        ) {
          throw new Error(
            "This history entry is no longer available for an upgrade.",
          );
        }
      }
      const job = await withInlineErrors(() =>
        sourceHistoryApi.upgrade(item.id, row?.history_id),
      );
      setQueued(job);
      try {
        const result = await awaitJobResult<{ upgraded: number }>(
          client,
          job.job_id,
          job.session,
        );
        if (!Number.isSafeInteger(result?.upgraded) || result.upgraded < 0) {
          throw new Error(
            "The upgrade job did not report a result. Check History or try again.",
          );
        }
        return result.upgraded;
      } finally {
        await refresh();
      }
    },
  });
  return { ...mutation, queued, allowed, canUpgrade };
}

export function SourceUpgradeFeedback({
  upgrade,
}: {
  upgrade: ReturnType<typeof useSourceUpgrade>;
}) {
  if (upgrade.isError) {
    return <Alert color="red">{sourceError(upgrade.error)}</Alert>;
  }
  if (upgrade.isPending) {
    return (
      <Text size="sm" role="status">
        {upgrade.queued
          ? "Upgrade queued. Waiting for the search to finish."
          : "Queuing upgrade..."}
      </Text>
    );
  }
  if (upgrade.isSuccess) {
    return (
      <Text size="sm" role="status">
        {upgrade.data === 0
          ? "No better subtitle was found."
          : `${upgrade.data} subtitle${upgrade.data === 1 ? "" : "s"} upgraded.`}
      </Text>
    );
  }
  return null;
}

export function SourceUpgradeButton({
  item,
  disabled = false,
}: {
  item: SourceItem;
  disabled?: boolean;
}) {
  const upgrade = useSourceUpgrade(item, disabled);
  return (
    <Stack gap="xs">
      <Toolbox.Button
        icon={faArrowUp}
        disabled={!upgrade.canUpgrade || upgrade.isPending}
        loading={upgrade.isPending}
        onClick={() => upgrade.mutate(undefined)}
      >
        Upgrade
      </Toolbox.Button>
      <SourceUpgradeFeedback upgrade={upgrade} />
    </Stack>
  );
}

export function SourceHistoryView({ item }: { item: SourceItem }) {
  const history = useSourceHistory(item);
  const current = useCurrentSourceItem(item);
  const upgrade = useSourceUpgrade(
    current.item ?? item,
    history.isError || !current.allowed,
  );
  const refresh = useRefreshSourceDetails();
  const exclude = useMutation({
    mutationFn: async (row: SourceHistoryRow) => {
      const live = current.item;
      if (
        !current.allowed ||
        !live ||
        history.isError ||
        !live.subtitles.some(
          (subtitle) =>
            sourceReleaseForSubtitle(history.data?.data, live, subtitle)
              ?.history_id === row.history_id,
        )
      ) {
        throw new Error("This subtitle is no longer available for exclusion.");
      }
      const freshItem = await sourceDetailApi.requireCurrentItem(item);
      const freshHistory = await withInlineErrors(() =>
        sourceHistoryApi.history(item.id),
      );
      const freshRow = freshHistory.data.find(
        (value) => value.history_id === row.history_id,
      );
      if (
        !freshRow ||
        !freshItem.subtitles.some(
          (subtitle) =>
            sourceReleaseForSubtitle(freshHistory.data, freshItem, subtitle)
              ?.history_id === freshRow.history_id,
        )
      ) {
        throw new Error("This subtitle is no longer available for exclusion.");
      }
      await withInlineErrors(() => sourceHistoryApi.exclude(item.id, freshRow));
    },
    onSuccess: refresh,
  });
  const columns: ColumnDef<SourceHistoryRow>[] = [
    {
      id: "action",
      cell: ({ row: { original } }) => (
        <Center>
          <HistoryIcon action={original.action} />
        </Center>
      ),
    },
    {
      header: "Language",
      accessorKey: "language",
      cell: ({ row: { original } }) =>
        original.language ? (
          <Badge>
            <Language.Text value={original.language} long />
          </Badge>
        ) : null,
    },
    {
      header: "Provider",
      accessorKey: "provider",
      cell: ({ row: { original } }) => (
        <HistoryProvider
          provider={original.provider ?? undefined}
          aiTranslated={original.ai_translated}
          action={original.action}
        />
      ),
    },
    { header: "Score", accessorKey: "score" },
    {
      id: "matches",
      cell: ({ row: { original } }) =>
        original.matches?.length || original.dont_matches?.length ? (
          <StateIcon
            matches={original.matches ?? []}
            dont={original.dont_matches ?? []}
            isHistory
          />
        ) : null,
    },
    {
      header: "Date",
      accessorKey: "timestamp",
      cell: ({ row: { original } }) => (
        <TextPopover text={original.parsed_timestamp}>
          <Text>{original.timestamp}</Text>
        </TextPopover>
      ),
    },
    {
      header: "Details",
      cell: ({ row: { original } }) => (
        <Stack gap={2}>
          <Text size="sm">{original.description}</Text>
          {original.upgradedFromId != null && (
            <Text size="xs" c="dimmed">
              Upgraded from history #{original.upgradedFromId}
            </Text>
          )}
        </Stack>
      ),
    },
    {
      id: "actions",
      cell: ({ row: { original } }) => {
        const release = current.item?.subtitles.some(
          (subtitle) =>
            sourceReleaseForSubtitle(
              history.data?.data,
              current.item!,
              subtitle,
            )?.history_id === original.history_id,
        );
        return (
          <Group gap="xs" wrap="nowrap">
            {original.upgradable && (
              <Action
                label="Upgrade"
                icon={faArrowUp}
                disabled={
                  !upgrade.allowed ||
                  !ownsHistory(original, item) ||
                  upgrade.isPending ||
                  exclude.isPending
                }
                onClick={() => upgrade.mutate(original)}
              />
            )}
            {original.provider &&
              original.subs_id &&
              original.language &&
              original.subtitles_path && (
                <Action
                  label="Exclude"
                  icon={faFileExcel}
                  disabled={
                    !current.allowed ||
                    history.isError ||
                    !release ||
                    original.blacklisted ||
                    exclude.isPending ||
                    upgrade.isPending
                  }
                  onClick={() => exclude.mutate(original)}
                />
              )}
          </Group>
        );
      },
    },
  ];
  return (
    <Stack gap="sm">
      {(history.isError || current.current.isError) && (
        <Alert color="red">
          {sourceError(history.error ?? current.current.error)}
        </Alert>
      )}
      {exclude.isError && (
        <Alert color="red">{sourceError(exclude.error)}</Alert>
      )}
      {exclude.isSuccess && (
        <Text role="status" size="sm">
          Subtitle excluded from search.
        </Text>
      )}
      <SourceUpgradeFeedback upgrade={upgrade} />
      <QueryOverlay result={history}>
        <PageTable
          columns={columns}
          data={history.isError ? [] : (history.data?.data ?? [])}
          getRowId={(row) => `source:${item.source_id}:${row.history_id}`}
          autoScroll={false}
          tableStyles={{
            emptyText: history.isError
              ? "History is unavailable"
              : "No history found",
          }}
        />
      </QueryOverlay>
    </Stack>
  );
}

export const SourceHistoryModal = withModal(
  SourceHistoryView,
  "source-item-history",
  { title: "Source Item History", size: "xl" },
);

export function SourceHistoryButton({
  item,
  disabled = false,
}: {
  item: SourceItem;
  disabled?: boolean;
}) {
  const modals = useModals();
  return (
    <Toolbox.Button
      icon={faHistory}
      disabled={disabled}
      onClick={() => modals.openContextModal(SourceHistoryModal, { item })}
    >
      History
    </Toolbox.Button>
  );
}
