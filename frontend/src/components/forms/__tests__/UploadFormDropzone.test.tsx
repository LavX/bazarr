/* eslint-disable camelcase */
import { StrictMode } from "react";
import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { ArchiveExtractResponse } from "@/apis/raw/subtitles";
import { MovieUploadModal } from "@/components/forms/MovieUploadForm";
import { SeriesUploadModal } from "@/components/forms/SeriesUploadForm";
import { ModalsProvider, useModals } from "@/modules/modals";
import { fireEvent, rawRender, screen, waitFor, within } from "@/tests";

const baseItem = {
  id: 560,
  arr_instance_id: 7,
  title: "Upload fixture",
  path: "/media/Upload fixture",
  profileId: 1,
  fanart: "",
  overview: "",
  imdbId: "tt0000001",
  alternativeTitles: [],
  poster: "",
  year: "2026",
  monitored: true,
  tags: [],
  audio_language: [],
};

const movie: Item.Movie = {
  ...baseItem,
  radarrId: 42,
  subtitles: [],
  missing_subtitles: [],
};

const series: Item.Series = {
  ...baseItem,
  sonarrSeriesId: 42,
  episodeFileCount: 4,
  episodeMissingCount: 4,
  ended: false,
  lastAired: "2026-01-01",
  seriesType: "Standard",
  tvdbId: 1234,
};

const episodes: Item.Episode[] = [1, 2, 3, 4].map((episode) => ({
  id: 900 + episode,
  series_id: series.id,
  sonarrSeriesId: series.sonarrSeriesId,
  sonarrEpisodeId: 1900 + episode,
  arr_instance_id: series.arr_instance_id,
  season: 1,
  episode,
  title: `Episode ${episode}`,
  path: `/media/S01E0${episode}.mkv`,
  monitored: true,
  subtitles: [],
  missing_subtitles: [],
  audio_language: [],
}));

const profile: Language.Profile = {
  profileId: 1,
  name: "Upload languages",
  cutoff: null,
  originalFormat: false,
  tag: undefined,
  mustContain: [],
  mustNotContain: [],
  items: ["en", "hu"].map((language, index) => ({
    id: index,
    language,
    forced: "False",
    hi: "False",
    audio_exclude: "False",
    audio_only_include: "False",
    translate_from: null,
  })),
};

type MediaKind = "movie" | "series";

function OpenUpload({ kind, file }: { kind: MediaKind; file: File }) {
  const modals = useModals();
  return (
    <button
      onClick={() => {
        if (kind === "movie") {
          modals.openContextModal(MovieUploadModal, { movie, files: [file] });
        } else {
          modals.openContextModal(SeriesUploadModal, {
            series,
            files: [file],
          });
        }
      }}
    >
      Open upload
    </button>
  );
}

function renderUpload(kind: MediaKind, file: File) {
  // jsdom has no layout. Mantine's documented test environment prevents its
  // floating dropdowns from hiding zero-size references; browser checks cover
  // portal placement and visibility with the production provider.
  return rawRender(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <MantineProvider env="test">
          <ModalsProvider>
            <OpenUpload kind={kind} file={file} />
          </ModalsProvider>
        </MantineProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}

function subtitle(episode: number) {
  return new File(
    ["1\n00:00:01,000 --> 00:00:02,000\nSubtitle fixture\n"],
    `S01E0${episode}.srt`,
  );
}

function dropFiles(target: HTMLElement, files: File[]) {
  fireEvent.drop(target, {
    dataTransfer: {
      files,
      items: files.map((file) => ({
        kind: "file",
        type: file.type,
        getAsFile: () => file,
      })),
      types: ["Files"],
    },
  });
}

function fileRow(name: string) {
  return screen.getByRole("row", { name: new RegExp(name) });
}

beforeEach(() => {
  // Detail pages already have these queries loaded before opening the modal.
  queryClient.setQueryData(
    [QueryKeys.System, QueryKeys.Languages, false],
    [
      { code2: "en", code3: "eng", name: "English", enabled: true },
      { code2: "hu", code3: "hun", name: "Hungarian", enabled: true },
    ],
  );
  queryClient.setQueryData(
    [QueryKeys.System, QueryKeys.LanguagesProfiles],
    [profile],
  );
  vi.spyOn(api.episodes, "bySeriesId").mockResolvedValue(episodes);
  vi.spyOn(api.subtitles, "info").mockImplementation(async (names) =>
    names.map((filename) => ({
      filename,
      season: 1,
      episode: Number(/E0(\d)/.exec(filename)?.[1]),
    })),
  );
  vi.spyOn(api.movies, "uploadSubtitles").mockResolvedValue(undefined);
  vi.spyOn(api.episodes, "uploadSubtitles").mockResolvedValue(undefined);
});

afterEach(() => vi.restoreAllMocks());

describe.each<MediaKind>(["movie", "series"])("%s upload Dropzone", (kind) => {
  it.each(["picker", "drop"])(
    "adds an entire %s batch without changing existing metadata",
    async (method) => {
      const user = userEvent.setup();
      const initial = subtitle(1);
      renderUpload(kind, initial);
      await user.click(screen.getByRole("button", { name: "Open upload" }));
      const dialog = await screen.findByRole("dialog");
      await waitFor(() => {
        expect(
          within(fileRow(initial.name)).getByDisplayValue("English"),
        ).toBeInTheDocument();
      });
      await user.click(
        within(fileRow(initial.name)).getByDisplayValue("English"),
      );
      await user.click(
        await screen.findByRole("option", { name: "Hungarian" }),
      );
      await user.click(
        within(fileRow(initial.name)).getByDisplayValue("Normal"),
      );
      await user.click(await screen.findByRole("option", { name: "Forced" }));
      if (kind === "series") {
        await user.click(await screen.findByDisplayValue("(1x1) Episode 1"));
        await user.click(
          await screen.findByRole("option", { name: "(1x2) Episode 2" }),
        );
      }

      const incoming = [subtitle(2), subtitle(3), subtitle(4)];
      Object.defineProperty(incoming[0], "webkitRelativePath", {
        value: `subtitles/${incoming[0].name}`,
      });
      if (method === "picker") {
        await user.upload(
          within(dialog).getByLabelText("file upload"),
          incoming,
        );
      } else {
        dropFiles(
          within(dialog).getByText(/Attach as many files as you like/),
          incoming,
        );
      }

      for (const file of incoming) {
        await waitFor(() => {
          expect(
            within(fileRow(file.name)).getByDisplayValue("English"),
          ).toBeInTheDocument();
          expect(
            within(fileRow(file.name)).getByDisplayValue("Normal"),
          ).toBeInTheDocument();
        });
      }
      expect(within(dialog).getAllByRole("row")).toHaveLength(5);
      expect(
        within(fileRow(initial.name)).getByDisplayValue("Hungarian"),
      ).toBeInTheDocument();
      expect(
        within(fileRow(initial.name)).getByDisplayValue("Forced"),
      ).toBeInTheDocument();
      if (kind === "series") {
        await waitFor(() => {
          expect(
            within(fileRow(initial.name)).getByDisplayValue("(1x2) Episode 2"),
          ).toBeInTheDocument();
          expect(
            within(fileRow(incoming[2].name)).getByDisplayValue(
              "(1x4) Episode 4",
            ),
          ).toBeInTheDocument();
        });
      }
      expect(api.movies.uploadSubtitles).not.toHaveBeenCalled();
      expect(api.episodes.uploadSubtitles).not.toHaveBeenCalled();

      await user.click(
        within(dialog).getByRole("button", { name: /^Upload$/ }),
      );
      const upload =
        kind === "movie"
          ? api.movies.uploadSubtitles
          : api.episodes.uploadSubtitles;
      await waitFor(() => expect(upload).toHaveBeenCalledTimes(4));
      const form = { file: initial, language: "hu", forced: true, hi: false };
      if (kind === "movie") {
        expect(api.movies.uploadSubtitles).toHaveBeenCalledWith(
          movie.radarrId,
          form,
          7,
        );
      } else {
        expect(api.episodes.uploadSubtitles).toHaveBeenCalledWith(
          series.sonarrSeriesId,
          1902,
          form,
          7,
        );
      }
      incoming.forEach((file, index) => {
        const addedForm = { file, language: "en", forced: false, hi: false };
        if (kind === "movie") {
          expect(api.movies.uploadSubtitles).toHaveBeenCalledWith(
            movie.radarrId,
            addedForm,
            7,
          );
        } else {
          expect(api.episodes.uploadSubtitles).toHaveBeenCalledWith(
            series.sonarrSeriesId,
            1902 + index,
            addedForm,
            7,
          );
        }
      });
      await waitFor(() =>
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
      );
    },
  );

  it("keeps a mixed archive batch pending until extracted rows are available", async () => {
    const user = userEvent.setup();
    let finishExtraction: (result: ArchiveExtractResponse) => void = () => {
      throw new Error("Archive request has not started");
    };
    vi.spyOn(api.subtitles, "extractArchive").mockImplementation(
      () =>
        new Promise((resolve) => {
          finishExtraction = resolve;
        }),
    );
    renderUpload(kind, subtitle(1));
    await user.click(screen.getByRole("button", { name: "Open upload" }));
    const dialog = await screen.findByRole("dialog");
    const archive = new File(["synthetic archive"], "subtitles.ZIP");
    dropFiles(within(dialog).getByText(/Attach as many files as you like/), [
      subtitle(2),
      archive,
    ]);
    await waitFor(() =>
      expect(api.subtitles.extractArchive).toHaveBeenCalledWith(archive),
    );
    expect(
      within(dialog).getByRole("button", { name: /^Upload$/ }),
    ).toBeDisabled();
    finishExtraction({
      files: [{ name: "S01E03.srt", content: btoa("Subtitle fixture") }],
      count: 1,
    });
    await waitFor(() => {
      expect(fileRow("S01E02.srt")).toBeInTheDocument();
      expect(fileRow("S01E03.srt")).toBeInTheDocument();
      expect(
        within(dialog).getByRole("button", { name: /^Upload$/ }),
      ).toBeEnabled();
    });
    expect(within(dialog).getAllByRole("row")).toHaveLength(4);
    expect(screen.queryByText(archive.name)).not.toBeInTheDocument();
    expect(api.movies.uploadSubtitles).not.toHaveBeenCalled();
    expect(api.episodes.uploadSubtitles).not.toHaveBeenCalled();
  });
});
