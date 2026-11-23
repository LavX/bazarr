/* eslint-disable camelcase */
import { useMemo, useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router";
import {
  Alert,
  Anchor,
  Badge,
  Breadcrumbs,
  Button,
  Container,
  Group,
  Pagination,
  Stack,
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import {
  faCloudUploadAlt,
  faHardDrive,
  faLanguage,
  faSearch,
  faToolbox,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { ColumnDef } from "@tanstack/react-table";
import { useMediaSources } from "@/apis/hooks/mediaSources";
import { useAppTitle } from "@/apis/hooks/site";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import { FullPageDropzone } from "@/components/inputs";
import SimpleTable from "@/components/tables/SimpleTable";
import { useModals, withModal } from "@/modules/modals";
import ItemOverview, { OverviewItem } from "@/pages/views/ItemOverview";
import { basename } from "@/utilities/files";
import {
  sameSourceItem,
  sourceDetailApi,
  sourceError,
  SourceGroup,
  SourceItem,
  sourceItemHasFile,
  useSourceEpisodes,
  useSourceGroup,
  useSourceItem,
  useSourceLeagueEvents,
  useSourceOwnerAvailable,
  useSourceScan,
  useSourceSearch,
  useSourceWrite,
} from "./api";
import {
  SourceDropUploadModal,
  SourceIdentityModal,
  SourceManualSearchModal,
  SourceUploadModal,
} from "./forms";
import { SourceHistoryButton, SourceUpgradeButton } from "./History";
import {
  OpenSourceEditor,
  SourceCombineButton,
  SourceSubtitleMenu,
} from "./SubtitleActions";
import { SourceToolsModal } from "./Tools";

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
                    item.kind,
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

export function SourceMovieActions({
  item,
  openEditor,
}: {
  item: SourceItem;
  openEditor: OpenSourceEditor;
}) {
  const modals = useModals();
  const search = useSourceSearch();
  const scan = useSourceScan();
  const available = useSourceOwnerAvailable(item);
  const filesAvailable =
    available && Boolean(item.path) && !item.server_missing;
  const hasFile = available && sourceItemHasFile(item);
  const ready =
    (item.effective_profile_id !== undefined
      ? item.effective_profile_id
      : item.profile_id) !== null &&
    hasFile &&
    ["identified", "parsed"].includes(item.identity_state);
  const manualReady = item.kind === "sports_event" ? filesAvailable : ready;
  // An episode's actions are the active modal body. The movie page stays
  // mounted underneath dialogs, where the foreground upload owns the drop.
  const acceptsPageDrop =
    ready && (item.kind === "episode" || modals.modals.length === 0);
  return (
    <Stack gap="xs">
      {item.kind !== "sports_event" && (
        <FullPageDropzone
          active={acceptsPageDrop}
          onDrop={(files) =>
            modals.openContextModal(SourceDropUploadModal, { item, files })
          }
        />
      )}
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
          disabled={!manualReady}
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
          disabled={!manualReady}
          onClick={() => modals.openContextModal(SourceUploadModal, { item })}
        >
          Upload
        </Toolbox.Button>
        <SourceProfileAction item={item} kind="items" />
        <SourceHistoryButton item={item} />
        <SourceUpgradeButton item={item} disabled={!ready} />
        <SourceCombineButton item={item} disabled={!hasFile} />
        <Toolbox.Button
          icon={faToolbox}
          disabled={
            !hasFile || !item.subtitles.some((subtitle) => subtitle.path)
          }
          onClick={() =>
            modals.openContextModal(SourceToolsModal, {
              targets: [item],
              openEditor,
            })
          }
        >
          Subtitle Tools
        </Toolbox.Button>
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
  const openEditor = useNavigate();
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
          icon={faToolbox}
          disabled={!available || item.kind !== "show"}
          onClick={() =>
            modals.openContextModal(SourceToolsModal, {
              targets: [{ ...item, kind: "show" }],
              openEditor,
            })
          }
        >
          Subtitle Tools
        </Toolbox.Button>
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

export function SourceSubtitleTable({
  item,
  openEditor,
}: {
  item: SourceItem;
  openEditor: OpenSourceEditor;
}) {
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
          row.original.path && sourceItemHasFile(item) && available ? (
            <SourceSubtitleMenu
              item={item}
              subtitle={row.original}
              openEditor={openEditor}
            />
          ) : null,
      },
    ],
    [item, available, openEditor],
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

function SourceEpisodeView({
  item,
  openEditor,
}: {
  item: SourceItem;
  openEditor: OpenSourceEditor;
}) {
  const query = useSourceItem(item.id, item.kind);
  const moved = query.data && !sameSourceItem(item, query.data);
  const current = !query.isError && !moved ? query.data : undefined;
  const sports = item.kind === "sports_event";
  if (query.data === null || moved) {
    return (
      <Alert color="red" role="alert">
        {sports
          ? "This event is no longer available."
          : "This episode moved or is no longer available."}
      </Alert>
    );
  }
  return (
    <QueryOverlay result={query}>
      <Stack>
        {query.isError && <Alert color="red">{sourceError(query.error)}</Alert>}
        {current && (
          <>
            <SourceMovieActions item={current} openEditor={openEditor} />
            <SourceSubtitleTable item={current} openEditor={openEditor} />
          </>
        )}
      </Stack>
    </QueryOverlay>
  );
}

const SourceEpisodeModal = withModal(SourceEpisodeView, "source-episode", {
  title: "Episode",
  size: "xl",
});

const SourceSportsEventModal = withModal(
  SourceEpisodeView,
  "source-sports-event",
  {
    title: "Event",
    size: "xl",
  },
);

function SourceSportsEventTitle({ event }: { event: SourceItem }) {
  const { data } = useSourceItem(event.id, "sports_event");
  return data === null || (data && !sameSourceItem(event, data))
    ? "Event unavailable"
    : (data?.title ?? event.title);
}

function SourceLeagueEvents({ id }: { id: number }) {
  const query = useSourceLeagueEvents(id);
  const modals = useModals();
  const openEditor = useNavigate();
  const seasons = useMemo(() => {
    const grouped = new Map<number | null, SourceItem[]>();
    for (const event of query.data ?? []) {
      const season = grouped.get(event.season) ?? [];
      season.push(event);
      grouped.set(event.season, season);
    }
    return [...grouped.entries()].sort(([a], [b]) => (b ?? -1) - (a ?? -1));
  }, [query.data]);
  const columns = useMemo<ColumnDef<SourceItem>[]>(
    () => [
      {
        header: "Event",
        accessorKey: "title",
        cell: ({ row: { original: event } }) => (
          <Group gap="xs">
            <Anchor
              component="button"
              className="table-primary"
              onClick={() =>
                modals.openContextModal(
                  SourceSportsEventModal,
                  { item: event, openEditor },
                  { title: <SourceSportsEventTitle event={event} /> },
                )
              }
            >
              {event.title}
            </Anchor>
            {event.arr_shadow && (
              <Badge color="gray">
                Managed by {event.shadowed_by || "Sportarr"}
              </Badge>
            )}
          </Group>
        ),
      },
      { header: "Round", accessorKey: "episode" },
      { header: "Part", accessorKey: "part_name" },
      { header: "Date", accessorKey: "event_date" },
      {
        header: "Missing Subtitles",
        id: "missing",
        cell: ({ row }) => (
          <Group gap="xs">
            {row.original.missing_subtitles.map((language, index) => (
              <Badge key={index} color="yellow">
                <Language.Text value={language} />
              </Badge>
            ))}
          </Group>
        ),
      },
    ],
    [modals, openEditor],
  );
  if (query.data === null) {
    return (
      <Alert color="red" role="alert">
        These events are no longer available.
      </Alert>
    );
  }
  return (
    <QueryOverlay result={query}>
      <Stack>
        {query.isError && <Alert color="red">{sourceError(query.error)}</Alert>}
        {query.isSuccess && seasons.length === 0 && <Text>No events</Text>}
        {seasons.map(([season, events]) => (
          <Stack key={season ?? "unknown"} gap="xs">
            <Text component="h2" fw={600}>
              {season === null ? "Unknown season" : `Season ${season}`}
            </Text>
            <SimpleTable
              columns={columns}
              data={events}
              getRowId={(event) => `source:${event.id}`}
            />
          </Stack>
        ))}
      </Stack>
    </QueryOverlay>
  );
}

function EpisodeList({ id }: { id: number }) {
  const [page, setPage] = useState(1);
  const query = useSourceEpisodes(id, page);
  const modals = useModals();
  const openEditor = useNavigate();
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
                { item: row.original, openEditor },
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
                { item: row.original, openEditor },
                { title: row.original.title },
              )
            }
          >
            Edit
          </Button>
        ),
      },
    ],
    [modals, openEditor],
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
  const openEditor = useNavigate();
  const query = useSourceItem(id);
  const item = !query.isError ? query.data : undefined;
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
              <SourceMovieActions item={item} openEditor={openEditor} />
            </Toolbox>
            <Stack>
              <SourceDetailBadge item={item} />
              <ItemOverview item={asOverview(item)} />
              <SourceSubtitleTable item={item} openEditor={openEditor} />
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

export function SourceLeagueDetailView() {
  const id = validId(useParams().id);
  const query = useSourceGroup(id, "league");
  const item = query.data;
  const unavailable = item === null;
  useDocumentTitle(
    `${item?.title ?? "Unknown League"} - ${useAppTitle()} (Sports)`,
  );
  if (!Number.isFinite(id) || (item && item.kind !== "league"))
    return <Navigate to="/not-found" />;
  return (
    <Container fluid px={0}>
      <nav aria-label="Breadcrumb">
        <Breadcrumbs mb="md" ml="xs">
          <Anchor component={Link} to="/sports" size="sm">
            Sports
          </Anchor>
          <Text size="sm">
            {item?.title ?? (unavailable ? "League unavailable" : "Loading...")}
          </Text>
        </Breadcrumbs>
      </nav>
      <QueryOverlay result={query}>
        {unavailable ? (
          <Alert color="red" role="alert">
            This league is no longer available.
          </Alert>
        ) : (
          query.isError && <Alert color="red">{sourceError(query.error)}</Alert>
        )}
        {item && (
          <Stack>
            <SourceDetailBadge item={item} />
            <ItemOverview item={asOverview(item)} />
            <SourceLeagueEvents id={id} />
          </Stack>
        )}
      </QueryOverlay>
    </Container>
  );
}
