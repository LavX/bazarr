import { FunctionComponent, useCallback, useRef } from "react";
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
import { Dropzone } from "@mantine/dropzone";
import { useDocumentTitle } from "@mantine/hooks";
import { showNotification } from "@mantine/notifications";
import {
  faCloudUploadAlt,
  faEllipsis,
  faHardDrive,
  faHistory,
  faLayerGroup,
  faMagnifyingGlass,
  faSearch,
  faSync,
  faToolbox,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { isNumber } from "lodash";
import {
  useDownloadMovieSubtitles,
  useIsMovieActionRunning,
  useMoviesProvider,
} from "@/apis/hooks";
import {
  useMovieAction,
  useMovieById,
  useMovieHistory,
  useMovieModification,
} from "@/apis/hooks/movies";
import { useInstanceName } from "@/apis/hooks/site";
import { Action, DropContent, Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import { CombineModal } from "@/components/forms/CombineForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MovieUploadModal } from "@/components/forms/MovieUploadForm";
import { MovieHistoryModal, SubtitleToolsModal } from "@/components/modals";
import { MovieSearchModal } from "@/components/modals/ManualSearchModal";
import { useModals } from "@/modules/modals";
import { notification, task, TaskGroup } from "@/modules/task";
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
  const { data: movieHistory } = useMovieHistory(movie?.radarrId);

  const profile = useLanguageProfileBy(movie?.profileId);

  const modals = useModals();

  const mutation = useMovieModification();
  const { mutateAsync: action } = useMovieAction();
  const { mutateAsync: downloadAsync } = useDownloadMovieSubtitles();

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
    `${movie?.title ?? "Unknown Movie"} - ${useInstanceName()} (Movies)`,
  );

  const openDropzone = useRef<VoidFunction>(null);

  if (isNaN(id) || (isFetched && !movie)) {
    return <Navigate to={RouterNames.NotFound}></Navigate>;
  }

  const allowEdit = movie?.profileId !== undefined;

  const availableLangs = (movie?.subtitles ?? [])
    .filter(
      (s) =>
        s.path &&
        !isSyncOutputSubtitle(s) &&
        !isCombinedOutputSubtitle(s) &&
        !s.hi &&
        !s.forced,
    )
    .map((s) => s.code2);

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
        <Dropzone.FullScreen
          openRef={openDropzone}
          active={profile !== undefined}
          onDrop={onDrop}
        >
          <DropContent></DropContent>
        </Dropzone.FullScreen>
        <Toolbox>
          <Group gap="xs">
            <Toolbox.Button
              icon={faSync}
              disabled={hasTask}
              onClick={async () => {
                if (movie) {
                  await action({
                    action: "sync",
                    radarrid: id,
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
                  task.create(movie.title, TaskGroup.ScanDisk, action, {
                    action: "scan-disk",
                    radarrid: id,
                  });
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
                    radarrid: id,
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
                    scope: { kind: "movie", radarrId: movie.radarrId },
                    availableLanguages: availableLangs,
                  });
                }
              }}
            >
              Combine Subtitles
            </Toolbox.Button>
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
          <ItemOverview item={movie ?? null} details={[]}></ItemOverview>
          <Table
            movie={movie ?? null}
            profile={profile}
            disabled={hasTask}
            history={movieHistory}
          ></Table>
        </Stack>
      </QueryOverlay>
    </Container>
  );
};

export default MovieDetailView;
