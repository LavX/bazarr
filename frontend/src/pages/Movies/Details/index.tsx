import {
  FunctionComponent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Link, Navigate, useParams } from "react-router";
import {
  Anchor,
  Breadcrumbs,
  Container,
  Group,
  Menu,
  Stack,
  Text,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { showNotification } from "@mantine/notifications";
import {
  faCloudUploadAlt,
  faDownload,
  faEllipsis,
  faHardDrive,
  faHistory,
  faLayerGroup,
  faMagnifyingGlass,
  faSearch,
  faServer,
  faSync,
  faToolbox,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQueryClient } from "@tanstack/react-query";
import { isNumber } from "lodash";
import {
  useDownloadMovieSubtitles,
  useIsMovieActionRunning,
  useMoviesProvider,
} from "@/apis/hooks";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { whenJobFinishes } from "@/apis/hooks/jobWatch";
import {
  useMovieAction,
  useMovieById,
  useMovieHistory,
  useMovieModification,
} from "@/apis/hooks/movies";
import { useAppTitle } from "@/apis/hooks/site";
import api from "@/apis/raw";
import { Action, FullPageDropzone, Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import { CombineModal } from "@/components/forms/CombineForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MovieUploadModal } from "@/components/forms/MovieUploadForm";
import { SubtitleDownloadModal } from "@/components/forms/SubtitleDownloadForm";
import { MovieHistoryModal, SubtitleToolsModal } from "@/components/modals";
import { MovieSearchModal } from "@/components/modals/ManualSearchModal";
import PlayOnMenu from "@/components/PlayOnMenu";
import { useModals } from "@/modules/modals";
import { notification } from "@/modules/notification";
import ItemOverview from "@/pages/views/ItemOverview";
import { RouterNames } from "@/Router/RouterNames";
import { useLanguageProfileBy } from "@/utilities/languages";
import {
  isCombinedOutputSubtitle,
  isSyncOutputSubtitle,
} from "@/utilities/subtitles";
import Table from "./table";

const MovieDetailView: FunctionComponent = () => {
  const param = useParams();
  const id = Number.parseInt(param.id ?? "");
  const movieQuery = useMovieById(id);
  const { data: movie, isFetched } = movieQuery;
  const { data: movieHistory } = useMovieHistory(movie?.id);
  const { multiInstance, nameById: instanceNameById } =
    useArrInstanceLabels("radarr");
  const overviewDetails =
    multiInstance && movie?.arr_instance_id != null
      ? [
          {
            icon: faServer,
            text:
              instanceNameById.get(movie.arr_instance_id) ??
              `#${movie.arr_instance_id}`,
          },
        ]
      : [];

  const profile = useLanguageProfileBy(movie?.profileId);

  const modals = useModals();

  const mutation = useMovieModification();
  const { mutateAsync: action } = useMovieAction();
  const { mutateAsync: downloadAsync } = useDownloadMovieSubtitles();
  const client = useQueryClient();
  // The title the background scan was started for, and how to stop following
  // it. One scan per visit to a title.
  const autoScan = useRef<{ id: number; stop: () => void } | null>(null);
  const [metadataPending, setMetadataPending] = useState(false);

  // A title indexed before codecs were stored has embedded rows the server
  // flags as codec_pending. Read its metadata in the background, without
  // blocking the page, and refresh the row when the scan job finishes. A row
  // whose codec is null on a current entry is not flagged: the format could
  // not be named then and a scan names it no better, so it is never rescanned.
  const needsMetadata = Boolean(
    movie?.subtitles.some(
      (subtitle) => !subtitle.path && subtitle.codec_pending,
    ),
  );
  const refetchMovie = movieQuery.refetch;

  useEffect(() => {
    if (!movie || !needsMetadata || autoScan.current?.id === id) {
      return;
    }
    setMetadataPending(true);
    let cancelled = false;
    let stopWatching: () => void = () => undefined;
    const scan = {
      id,
      stop: () => {
        cancelled = true;
        stopWatching();
      },
    };
    autoScan.current = scan;
    const done = () => {
      if (cancelled) {
        return;
      }
      setMetadataPending(false);
      void refetchMovie();
    };
    api.movies
      .action({
        action: "scan-disk",
        radarrid: movie.radarrId,
        arr_instance_id: movie.arr_instance_id,
      })
      .then((result) => {
        if (cancelled) {
          return;
        }
        const jobId = result?.job_id;
        if (typeof jobId === "number") {
          stopWatching = whenJobFinishes(client, jobId, done);
        } else {
          done();
        }
      })
      .catch(() => {
        if (!cancelled) {
          setMetadataPending(false);
        }
      });
    // No cleanup here: the movie refetching with its codecs filled in flips
    // needsMetadata, which re-runs this effect, and that must not end the
    // watch on the job. Leaving the title does, below.
  }, [client, id, movie, needsMetadata, refetchMovie]);

  // A refetch can fill every codec before the job's terminal event arrives:
  // nothing is pending then, whatever the job still says.
  useEffect(() => {
    if (!needsMetadata) {
      setMetadataPending(false);
    }
  }, [needsMetadata]);

  // Leaving the title, or the page, stops following its scan.
  useEffect(
    () => () => {
      autoScan.current?.stop();
      autoScan.current = null;
    },
    [id],
  );

  const download = useCallback(
    (item: Item.Movie, result: SearchResultType) => {
      const {
        language,
        hearing_impaired: hi,
        forced,
        provider,
        subtitle,
        original_format: originalFormat,
      } = result;
      const { radarrId } = item;

      return downloadAsync({
        radarrId,
        // Scope the download to the movie's owning instance (#156).
        arrInstanceId: item.arr_instance_id,
        form: {
          language,
          hi,
          forced,
          provider,
          subtitle,
          // eslint-disable-next-line camelcase
          original_format: originalFormat,
        },
      });
    },
    [downloadAsync],
  );

  const onDrop = useCallback(
    (files: File[]) => {
      if (movie && profile) {
        modals.openContextModal(MovieUploadModal, {
          files,
          movie,
        });
      } else {
        showNotification(
          notification.warn(
            "Cannot Upload Files",
            "movie or language profile is not ready",
          ),
        );
      }
    },
    [modals, movie, profile],
  );

  const hasTask = useIsMovieActionRunning();

  useDocumentTitle(
    `${movie?.title ?? "Unknown Movie"} - ${useAppTitle()} (Movies)`,
  );

  const openDropzone = useRef<VoidFunction>(null);

  if (isNaN(id) || (isFetched && !movie)) {
    return <Navigate to={RouterNames.NotFound}></Navigate>;
  }

  const allowEdit = movie?.profileId !== undefined;

  const availableLangs = Array.from(
    new Set(
      (movie?.subtitles ?? [])
        .filter(
          (s) =>
            s.path && !isSyncOutputSubtitle(s) && !isCombinedOutputSubtitle(s),
        )
        .map((s) => s.code2),
    ),
  );

  // Every base language with a file on disk, sync/combined outputs included:
  // the bundle download ships whatever exists, not just original subtitles.
  const downloadableLangs = Array.from(
    new Set((movie?.subtitles ?? []).filter((s) => s.path).map((s) => s.code2)),
  ).sort();

  return (
    <Container fluid px={0}>
      <nav aria-label="Breadcrumb">
        <Breadcrumbs mb="md" ml="xs">
          <Anchor component={Link} to="/movies" size="sm">
            Movies
          </Anchor>
          <Text size="sm" c="var(--bz-text-primary)">
            {movie?.title ?? "Loading..."}
          </Text>
        </Breadcrumbs>
      </nav>
      <QueryOverlay result={movieQuery}>
        <FullPageDropzone
          openRef={openDropzone}
          active={profile !== undefined}
          onDrop={onDrop}
        />
        <Toolbox wrapOnPhone>
          <Group gap="xs">
            <Toolbox.Button
              icon={faSync}
              disabled={hasTask}
              onClick={async () => {
                if (movie) {
                  await action({
                    action: "sync",
                    radarrid: movie.radarrId,
                    arr_instance_id: movie.arr_instance_id,
                  });
                }
              }}
            >
              Sync
            </Toolbox.Button>
            <Toolbox.Button
              icon={faHardDrive}
              disabled={hasTask}
              onClick={() => {
                if (movie) {
                  // Queued as a backend job: the request returns at once and the
                  // scan reports through the jobs drawer. HTTP errors are
                  // already reported by the API client.
                  action({
                    action: "scan-disk",
                    radarrid: movie.radarrId,
                    arr_instance_id: movie.arr_instance_id,
                  }).catch(() => undefined);
                }
              }}
            >
              Scan Disk
            </Toolbox.Button>
            <Toolbox.Button
              icon={faSearch}
              disabled={!isNumber(movie?.profileId)}
              loading={hasTask}
              onClick={async () => {
                if (movie) {
                  await action({
                    action: "search-missing",
                    radarrid: movie.radarrId,
                    arr_instance_id: movie.arr_instance_id,
                  });
                }
              }}
            >
              Search
            </Toolbox.Button>
            <Toolbox.Button
              icon={faMagnifyingGlass}
              disabled={!isNumber(movie?.profileId) || hasTask}
              onClick={() => {
                if (movie) {
                  modals.openContextModal(MovieSearchModal, {
                    item: movie,
                    download,
                    query: useMoviesProvider,
                  });
                }
              }}
            >
              Manual Search
            </Toolbox.Button>
            <Toolbox.Button
              icon={faLayerGroup}
              disabled={availableLangs.length < 2}
              onClick={() => {
                if (movie) {
                  modals.openContextModal(CombineModal, {
                    scope: {
                      kind: "movie",
                      radarrId: movie.radarrId,
                      arrInstanceId: movie.arr_instance_id ?? undefined,
                    },
                    availableLanguages: availableLangs,
                  });
                }
              }}
            >
              Combine Subtitles
            </Toolbox.Button>
            {movie && (
              <PlayOnMenu
                mediaType="movie"
                id={movie.id}
                itemLabel={movie.title}
                variant="toolbox"
              />
            )}
          </Group>
          <Group gap="xs">
            <Toolbox.Button
              disabled={!allowEdit || movie.profileId === null || hasTask}
              icon={faCloudUploadAlt}
              onClick={() => openDropzone.current?.()}
            >
              Upload
            </Toolbox.Button>
            <Toolbox.Button
              disabled={downloadableLangs.length === 0}
              icon={faDownload}
              onClick={() => {
                if (movie) {
                  modals.openContextModal(SubtitleDownloadModal, {
                    scope: {
                      kind: "movie",
                      radarrId: movie.radarrId,
                      arrInstanceId: movie.arr_instance_id ?? undefined,
                    },
                    availableLanguages: downloadableLangs,
                  });
                }
              }}
            >
              Download
            </Toolbox.Button>
            <Toolbox.Button
              icon={faWrench}
              disabled={hasTask}
              onClick={() => {
                if (movie) {
                  modals.openContextModal(
                    ItemEditModal,
                    {
                      item: movie,
                      mutation,
                    },
                    { title: movie.title },
                  );
                }
              }}
            >
              Edit Movie
            </Toolbox.Button>
            <Menu>
              <Menu.Target>
                <Action
                  label="More Actions"
                  icon={faEllipsis}
                  disabled={hasTask}
                />
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faToolbox} />}
                  onClick={() => {
                    if (movie) {
                      modals.openContextModal(SubtitleToolsModal, {
                        payload: [movie],
                      });
                    }
                  }}
                >
                  Mass Edit
                </Menu.Item>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faHistory} />}
                  onClick={() => {
                    if (movie) {
                      modals.openContextModal(MovieHistoryModal, { movie });
                    }
                  }}
                >
                  History
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Toolbox>
        <Stack>
          <ItemOverview
            item={movie ?? null}
            details={overviewDetails}
          ></ItemOverview>
          <Table
            movie={movie ?? null}
            profile={profile}
            disabled={hasTask}
            history={movieHistory}
            metadataPending={metadataPending}
          ></Table>
        </Stack>
      </QueryOverlay>
    </Container>
  );
};

export default MovieDetailView;
