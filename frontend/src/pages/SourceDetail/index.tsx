/* eslint-disable camelcase */
import { useMemo, useState } from "react";
import { Link, Navigate, useParams } from "react-router";
import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Container,
  Group,
  Menu,
  Pagination,
  Stack,
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faCloudUploadAlt,
  faEllipsis,
  faHardDrive,
  faLanguage,
  faSearch,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { ColumnDef } from "@tanstack/react-table";
import { useMediaSources } from "@/apis/hooks/mediaSources";
import { useAppTitle } from "@/apis/hooks/site";
import { Action, Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import SimpleTable from "@/components/tables/SimpleTable";
import { useModals, withModal } from "@/modules/modals";
import ItemOverview, { OverviewItem } from "@/pages/views/ItemOverview";
import { basename } from "@/utilities/files";
import {
  sourceDetailApi,
  sourceError,
  SourceGroup,
  SourceItem,
  useSourceEpisodes,
  useSourceGroup,
  useSourceItem,
  useSourceOwnerAvailable,
  useSourceScan,
  useSourceSearch,
  useSourceWrite,
} from "./api";
import {
  SourceIdentityModal,
  SourceManualSearchModal,
  SourceUploadModal,
} from "./forms";

function asOverview(item: SourceGroup | SourceItem): OverviewItem {
  return {
    title: item.title,
    year: item.year ?? "",
    path: item.path ?? "",
    poster: item.poster ?? "",
    profileId:
      item.effective_profile_id !== undefined
        ? item.effective_profile_id
        : item.profile_id,
    monitored: item.monitored,
    imdbId: item.imdb_id ?? "",
    tags: [],
    audio_language: "audio_language" in item ? (item.audio_language ?? []) : [],
    fanart: "",
    overview: "",
    alternativeTitles: [],
  };
}

function SourceDetailBadge({ item }: { item: SourceGroup | SourceItem }) {
  const { data } = useMediaSources();
  const source = data?.data.find((value) => value.id === item.source_id);
  return (
    <SourceBadge
      row={{
        origin: "source",
        source_key: `source:${item.source_id}`,
        source_name: source?.name ?? null,
        source_kind: source?.kind,
      }}
      labels={new Map()}
      arrNames={new Map()}
      defaultArr={null}
    />
  );
}

export function SourceProfileAction({
  item,
  kind,
}: {
  item: SourceGroup | SourceItem;
  kind: "groups" | "items";
}) {
  const modals = useModals();
  const write = useSourceWrite();
  return (
    <Stack gap="xs">
      <Toolbox.Button
        icon={faLanguage}
        loading={write.isPending}
        onClick={() =>
          modals.openContextModal(ChangeProfileModal, {
            onSelect: (profileId) => {
              void write
                .mutateAsync(() =>
                  sourceDetailApi.updateProfile(
                    kind,
                    item.id,
                    profileId,
                    item.kind === "show"
                      ? "show"
                      : item.kind === "episode"
                        ? "episode"
                        : "movie",
                  ),
                )
                .catch(() => undefined);
            },
          })
        }
      >
        Change Profile
      </Toolbox.Button>
      {write.isError && <Alert color="red">{sourceError(write.error)}</Alert>}
    </Stack>
  );
}

export function SourceMovieActions({ item }: { item: SourceItem }) {
  const modals = useModals();
  const search = useSourceSearch();
  const scan = useSourceScan();
  const available = useSourceOwnerAvailable(item);
  const ready =
    (item.effective_profile_id !== undefined
      ? item.effective_profile_id
      : item.profile_id) !== null &&
    !item.server_missing &&
    available &&
    !item.arr_shadow &&
    ["identified", "parsed"].includes(item.identity_state);
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Toolbox.Button
          icon={faSearch}
          loading={search.isPending}
          disabled={!ready}
          onClick={() => search.mutate(item.id)}
        >
          Search
        </Toolbox.Button>
        <Toolbox.Button
          icon={faSearch}
          disabled={!ready}
          onClick={() =>
            modals.openContextModal(SourceManualSearchModal, { item })
          }
        >
          Manual Search
        </Toolbox.Button>
        <Toolbox.Button
          icon={faHardDrive}
          loading={scan.isPending}
          disabled={!available}
          onClick={() => scan.mutate(item)}
        >
          Scan Disk
        </Toolbox.Button>
        <Toolbox.Button
          icon={faCloudUploadAlt}
          disabled={!ready}
          onClick={() => modals.openContextModal(SourceUploadModal, { item })}
        >
          Upload
        </Toolbox.Button>
        <SourceProfileAction item={item} kind="items" />
        <Toolbox.Button
          icon={faWrench}
          onClick={() =>
            modals.openContextModal(SourceIdentityModal, {
              item,
              kind: "items",
            })
          }
        >
          Identity fix
        </Toolbox.Button>
      </Group>
      {search.isError && <Alert color="red">{sourceError(search.error)}</Alert>}
      {scan.isError && <Alert color="red">{sourceError(scan.error)}</Alert>}
    </Stack>
  );
}

export function SourceSeriesActions({ item }: { item: SourceGroup }) {
  const modals = useModals();
  const scan = useSourceScan();
  const search = useSourceSearch("groups");
  const available = useSourceOwnerAvailable(item);
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Toolbox.Button
          icon={faSearch}
          loading={search.isPending}
          disabled={!available}
          onClick={() => search.mutate(item.id)}
        >
          Search
        </Toolbox.Button>
        <Toolbox.Button
          icon={faHardDrive}
          loading={scan.isPending}
          disabled={!available}
          onClick={() => scan.mutate(item)}
        >
          Scan Disk
        </Toolbox.Button>
        <SourceProfileAction item={item} kind="groups" />
        <Toolbox.Button
          icon={faWrench}
          onClick={() =>
            modals.openContextModal(SourceIdentityModal, {
              item,
              kind: "groups",
            })
          }
        >
          Identity fix
        </Toolbox.Button>
      </Group>
      {scan.isError && <Alert color="red">{sourceError(scan.error)}</Alert>}
      {search.isError && <Alert color="red">{sourceError(search.error)}</Alert>}
    </Stack>
  );
}

function SourceSubtitleMenu({
  item,
  subtitle,
}: {
  item: SourceItem;
  subtitle: Subtitle;
}) {
  const modals = useModals();
  const write = useSourceWrite();
  return (
    <Stack gap="xs">
      <Menu position="bottom-end">
        <Menu.Target>
          <Action
            label="Actions"
            icon={faEllipsis}
            disabled={write.isPending}
          />
        </Menu.Target>
        <Menu.Dropdown>
          <Menu.Item
            color="red"
            onClick={() =>
              modals.openConfirmModal({
                title: "Delete Subtitle",
                children: <Text>{basename(subtitle.path ?? "")}</Text>,
                labels: { confirm: "Delete", cancel: "Cancel" },
                confirmProps: { color: "red" },
                onConfirm: () =>
                  write.mutate(() =>
                    sourceDetailApi.removeSubtitle(item.id, subtitle),
                  ),
              })
            }
          >
            Delete
          </Menu.Item>
        </Menu.Dropdown>
      </Menu>
      {write.isError && <Alert color="red">{sourceError(write.error)}</Alert>}
    </Stack>
  );
}

export function SourceSubtitleTable({ item }: { item: SourceItem }) {
  const available = useSourceOwnerAvailable(item);
  const columns = useMemo<ColumnDef<Subtitle>[]>(
    () => [
      {
        header: "Language",
        accessorKey: "code2",
        cell: ({ row }) => (
          <Badge>
            <Language.Text value={row.original} />
          </Badge>
        ),
      },
      {
        header: "Type",
        id: "type",
        cell: ({ row }) => (row.original.path ? "External" : "Embedded"),
      },
      {
        header: "File",
        accessorKey: "path",
        cell: ({ row }) => (
          <Text size="sm" style={{ overflowWrap: "anywhere" }}>
            {row.original.path
              ? basename(row.original.path)
              : (row.original.codec ?? "-")}
          </Text>
        ),
      },
      {
        id: "actions",
        cell: ({ row }) =>
          row.original.path && !item.arr_shadow && available ? (
            <SourceSubtitleMenu item={item} subtitle={row.original} />
          ) : null,
      },
    ],
    [item, available],
  );
  return (
    <Stack gap="xs">
      <SimpleTable
        columns={columns}
        data={item.subtitles ?? []}
        getRowId={(row, index) =>
          `${row.code2}:${row.hi}:${row.forced}:${row.path ?? index}`
        }
        tableStyles={{ emptyText: "No subtitles" }}
      />
      {(item.missing_subtitles ?? []).length > 0 && (
        <Group>
          <Text size="sm">Missing Subtitles</Text>
          {item.missing_subtitles.map((language, index) => (
            <Badge color="yellow" key={index}>
              <Language.Text value={language} />
            </Badge>
          ))}
        </Group>
      )}
    </Stack>
  );
}

function SourceEpisodeView({ item }: { item: SourceItem }) {
  const query = useSourceItem(item.id, "episode");
  const current = query.data ?? item;
  return (
    <QueryOverlay result={query}>
      <Stack>
        {query.isError && <Alert color="red">{sourceError(query.error)}</Alert>}
        <SourceMovieActions item={current} />
        <SourceSubtitleTable item={current} />
      </Stack>
    </QueryOverlay>
  );
}

const SourceEpisodeModal = withModal(SourceEpisodeView, "source-episode", {
  title: "Episode",
  size: "xl",
});

function EpisodeList({ id }: { id: number }) {
  const [page, setPage] = useState(1);
  const query = useSourceEpisodes(id, page);
  const modals = useModals();
  const columns = useMemo<ColumnDef<SourceItem>[]>(
    () => [
      {
        header: "Episode",
        id: "episode",
        cell: ({ row }) =>
          row.original.season != null && row.original.episode != null
            ? `${row.original.season}x${String(row.original.episode).padStart(2, "0")}`
            : "-",
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row }) => (
          <Anchor
            component="button"
            className="table-primary"
            onClick={() =>
              modals.openContextModal(
                SourceEpisodeModal,
                { item: row.original },
                { title: row.original.title },
              )
            }
          >
            {row.original.title}
          </Anchor>
        ),
      },
      {
        header: "Missing Subtitles",
        id: "missing",
        cell: ({ row }) => (
          <Group gap="xs">
            {(row.original.missing_subtitles ?? []).map((language, index) => (
              <Badge key={index} color="yellow">
                <Language.Text value={language} />
              </Badge>
            ))}
          </Group>
        ),
      },
      {
        id: "actions",
        cell: ({ row }) => (
          <Button
            size="compact-xs"
            variant="subtle"
            onClick={() =>
              modals.openContextModal(
                SourceEpisodeModal,
                { item: row.original },
                { title: row.original.title },
              )
            }
          >
            Edit
          </Button>
        ),
      },
    ],
    [modals],
  );
  return (
    <QueryOverlay result={query}>
      <Stack>
        {query.isError && <Alert color="red">{sourceError(query.error)}</Alert>}
        <SimpleTable
          columns={columns}
          data={query.data?.data ?? []}
          getRowId={(item) => `source:${item.id}`}
          tableStyles={{ emptyText: "No episodes" }}
        />
        {(query.data?.total ?? 0) > 50 && (
          <Pagination
            value={page}
            onChange={setPage}
            total={Math.ceil((query.data?.total ?? 0) / 50)}
          />
        )}
      </Stack>
    </QueryOverlay>
  );
}

function validId(value: string | undefined) {
  return value &&
    /^[1-9][0-9]*$/.test(value) &&
    Number.isSafeInteger(Number(value))
    ? Number(value)
    : NaN;
}

export function SourceMovieDetailView() {
  const id = validId(useParams().id);
  const query = useSourceItem(id);
  const item = query.data;
  useDocumentTitle(
    `${item?.title ?? "Unknown Movie"} - ${useAppTitle()} (Movies)`,
  );
  if (!Number.isFinite(id) || (item && item.kind !== "movie"))
    return <Navigate to="/not-found" />;
  return (
    <Container fluid px={0}>
      <nav aria-label="Breadcrumb">
        <Breadcrumbs mb="md" ml="xs">
          <Anchor component={Link} to="/movies" size="sm">
            Movies
          </Anchor>
          <Text size="sm">{item?.title ?? "Loading..."}</Text>
        </Breadcrumbs>
      </nav>
      <QueryOverlay result={query}>
        {query.isError && <Alert color="red">{sourceError(query.error)}</Alert>}
        {item && (
          <>
            <Toolbox wrapOnPhone>
              <SourceMovieActions item={item} />
            </Toolbox>
            <Stack>
              <SourceDetailBadge item={item} />
              <ItemOverview item={asOverview(item)} />
              <SourceSubtitleTable item={item} />
            </Stack>
          </>
        )}
      </QueryOverlay>
    </Container>
  );
}

export function SourceSeriesDetailView() {
  const id = validId(useParams().id);
  const query = useSourceGroup(id);
  const item = query.data;
  useDocumentTitle(
    `${item?.title ?? "Unknown Series"} - ${useAppTitle()} (Series)`,
  );
  if (!Number.isFinite(id) || (item && item.kind !== "show"))
    return <Navigate to="/not-found" />;
  return (
    <Container fluid px={0}>
      <nav aria-label="Breadcrumb">
        <Breadcrumbs mb="md" ml="xs">
          <Anchor component={Link} to="/series" size="sm">
            Series
          </Anchor>
          <Text size="sm">{item?.title ?? "Loading..."}</Text>
        </Breadcrumbs>
      </nav>
      <QueryOverlay result={query}>
        {query.isError && <Alert color="red">{sourceError(query.error)}</Alert>}
        {item && (
          <>
            <Toolbox wrapOnPhone>
              <SourceSeriesActions item={item} />
            </Toolbox>
            <Stack>
              <SourceDetailBadge item={item} />
              <ItemOverview item={asOverview(item)} />
              <EpisodeList key={id} id={id} />
            </Stack>
          </>
        )}
      </QueryOverlay>
    </Container>
  );
}
