/* eslint-disable camelcase */
import { StrictMode, useRef } from "react";
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
import { FullPageDropzone } from "@/components/inputs/FullPageDropzone";
import { ModalsProvider, useModals } from "@/modules/modals";
import { act, fireEvent, rawRender, screen, waitFor, within } from "@/tests";

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

function UploadPage({ kind }: { kind: MediaKind }) {
  const modals = useModals();
  const openRef = useRef<(() => void) | null>(null);
  const onDrop = (files: File[]) => {
    if (kind === "movie") {
      modals.openContextModal(MovieUploadModal, { movie, files });
    } else {
      modals.openContextModal(SeriesUploadModal, { series, files });
    }
  };
  return (
    <div data-testid="page-drop-target">
      <FullPageDropzone active onDrop={onDrop} openRef={openRef} />
      <span data-testid="modal-count">{modals.modals.length}</span>
      <button onClick={() => openRef.current?.()}>Upload subtitles</button>
    </div>
  );
}

function renderUpload(kind: MediaKind) {
  // jsdom has no layout. Mantine's documented test environment prevents its
  // floating dropdowns from hiding zero-size references; browser checks cover
  // portal placement and visibility with the production provider.
  return rawRender(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <MantineProvider env="test">
          <ModalsProvider>
            <UploadPage kind={kind} />
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

// react-dropzone reads DataTransfer.items on every drag event, not only on
// drop, so a transfer without them throws inside file-selector before the
// component ever sees the drag. One representation for every drag phase.
function fileTransfer(files: File[]) {
  return {
    files,
    items: files.map((file) => ({
      kind: "file",
      type: file.type,
      getAsFile: () => file,
    })),
    types: ["Files"],
  };
}

function dropFiles(target: HTMLElement, files: File[]) {
  fireEvent.drop(target, { dataTransfer: fileTransfer(files) });
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

const overlayText =
  "Drop subtitle files or a .zip / .rar / .7z archive to upload";

function dragFiles(
  target: HTMLElement,
  type: "dragEnter" | "dragLeave",
  files: File[],
) {
  fireEvent[type](target, { dataTransfer: fileTransfer(files) });
}

async function openFromWindow(kind: MediaKind) {
  renderUpload(kind);
  const page = screen.getByTestId("page-drop-target");
  dragFiles(page, "dragEnter", [subtitle(1)]);
  expect(screen.getByText(overlayText)).toBeInTheDocument();
  dropFiles(page, [subtitle(1)]);
  const dialog = await screen.findByRole("dialog");
  await waitFor(() =>
    expect(
      within(fileRow("S01E01.srt")).getByDisplayValue("English"),
    ).toBeInTheDocument(),
  );
  expect(screen.getByTestId("modal-count")).toHaveTextContent("1");
  expect(screen.queryByText(overlayText)).not.toBeInTheDocument();
  return dialog;
}

// Mantine 9.5 does not expose accessible names for these native controls.
function nativeControl<T extends HTMLElement>(
  scope: HTMLElement,
  selector: string,
): T {
  // eslint-disable-next-line testing-library/no-node-access
  const control = scope.querySelector<T>(selector);
  expect(control).not.toBeNull();
  return control!;
}

async function closeOnce(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
) {
  await user.click(
    nativeControl<HTMLButtonElement>(dialog, ".mantine-Modal-close"),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(screen.getByTestId("modal-count")).toHaveTextContent("0");
}

describe.each<MediaKind>(["movie", "series"])(
  "%s full-page upload flow",
  (kind) => {
    it("adds repeated nested drops once, retains metadata and closes once after dragging from outside", async () => {
      const user = userEvent.setup();
      const dialog = await openFromWindow(kind);
      const initial = fileRow("S01E01.srt");
      await user.click(within(initial).getByDisplayValue("English"));
      await user.click(
        await screen.findByRole("option", { name: "Hungarian" }),
      );
      await user.click(within(initial).getByDisplayValue("Normal"));
      await user.click(await screen.findByRole("option", { name: "Forced" }));
      if (kind === "series") {
        await user.click(await screen.findByDisplayValue("(1x1) Episode 1"));
        await user.click(
          await screen.findByRole("option", { name: "(1x2) Episode 2" }),
        );
      }
      const dropTarget = within(dialog).getByText(
        /Attach as many files as you like/,
      );
      const page = screen.getByTestId("page-drop-target");
      dragFiles(page, "dragEnter", [subtitle(2)]);
      expect(screen.getByText(overlayText)).toBeInTheDocument();
      dragFiles(dropTarget, "dragEnter", [subtitle(2)]);
      dragFiles(page, "dragLeave", [subtitle(2)]);
      dropFiles(dropTarget, [subtitle(2), subtitle(3)]);
      await waitFor(() => expect(fileRow("S01E03.srt")).toBeInTheDocument());
      expect(screen.queryByText(overlayText)).not.toBeInTheDocument();
      expect(screen.getByTestId("modal-count")).toHaveTextContent("1");
      dropFiles(dropTarget, [subtitle(4)]);
      await waitFor(() => expect(fileRow("S01E04.srt")).toBeInTheDocument());
      expect(within(dialog).getAllByRole("row")).toHaveLength(5);
      expect(
        within(fileRow("S01E01.srt")).getByDisplayValue("Hungarian"),
      ).toBeInTheDocument();
      expect(
        within(fileRow("S01E01.srt")).getByDisplayValue("Forced"),
      ).toBeInTheDocument();
      if (kind === "series") {
        expect(
          within(fileRow("S01E01.srt")).getByDisplayValue("(1x2) Episode 2"),
        ).toBeInTheDocument();
      }
      expect(api.movies.uploadSubtitles).not.toHaveBeenCalled();
      expect(api.episodes.uploadSubtitles).not.toHaveBeenCalled();
      await closeOnce(user, dialog);
      dropFiles(page, [subtitle(2)]);
      const reopened = await screen.findByRole("dialog");
      await waitFor(() => expect(fileRow("S01E02.srt")).toBeInTheDocument());
      expect(within(reopened).getAllByRole("row")).toHaveLength(2);
      await closeOnce(user, reopened);
    });

    it("preserves the toolbar picker and a nested picker batch without adding a second modal", async () => {
      const user = userEvent.setup();
      renderUpload(kind);
      const input = nativeControl<HTMLInputElement>(
        screen.getByTestId("page-drop-target"),
        "input[type=file]",
      );
      const clicked = vi.fn();
      input.addEventListener("click", clicked);
      await user.click(
        screen.getByRole("button", { name: "Upload subtitles" }),
      );
      expect(clicked).toHaveBeenCalledOnce();
      await user.upload(input, [subtitle(1)]);
      const dialog = await screen.findByRole("dialog");
      await waitFor(() => expect(fileRow("S01E01.srt")).toBeInTheDocument());
      const nestedInput = nativeControl<HTMLInputElement>(
        dialog,
        "input[type=file]",
      );
      await user.upload(nestedInput, [subtitle(2), subtitle(3)]);
      await waitFor(() => expect(fileRow("S01E03.srt")).toBeInTheDocument());
      expect(within(dialog).getAllByRole("row")).toHaveLength(4);
      expect(screen.getByTestId("modal-count")).toHaveTextContent("1");
      await closeOnce(user, dialog);
    });

    it("does not open another modal when a nested drop occurs during archive extraction", async () => {
      const user = userEvent.setup();
      let finish: ((value: ArchiveExtractResponse) => void) | undefined;
      vi.spyOn(api.subtitles, "extractArchive").mockImplementation(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      );
      const dialog = await openFromWindow(kind);
      const dropTarget = within(dialog).getByText(
        /Attach as many files as you like/,
      );
      const archive = new File(["synthetic archive"], "subtitles.zip");
      // The picker starts extraction without relying on the nested drag fix.
      await user.upload(
        nativeControl<HTMLInputElement>(dialog, "input[type=file]"),
        [archive],
      );
      await waitFor(() =>
        expect(api.subtitles.extractArchive).toHaveBeenCalledOnce(),
      );
      expect(
        within(dialog).getByRole("button", { name: /^Upload$/ }),
      ).toBeDisabled();
      const loadingOverlay = nativeControl<HTMLElement>(
        dialog,
        ".mantine-LoadingOverlay-root",
      );
      dragFiles(loadingOverlay, "dragEnter", [subtitle(4)]);
      expect(screen.getByText(overlayText)).toBeInTheDocument();
      dropFiles(loadingOverlay, [subtitle(4)]);
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.queryByText(overlayText)).not.toBeInTheDocument();
      expect(screen.queryByText("S01E04.srt")).not.toBeInTheDocument();
      expect(screen.getByTestId("modal-count")).toHaveTextContent("1");
      await act(async () =>
        finish!({
          files: [{ name: "S01E02.srt", content: btoa("Subtitle fixture") }],
          count: 1,
        }),
      );
      await waitFor(() => expect(fileRow("S01E02.srt")).toBeInTheDocument());
      expect(
        within(dialog).getByRole("button", { name: /^Upload$/ }),
      ).toBeEnabled();
      expect(screen.queryByText(overlayText)).not.toBeInTheDocument();
      dropFiles(dropTarget, [subtitle(3)]);
      await waitFor(() => expect(fileRow("S01E03.srt")).toBeInTheDocument());
      expect(within(dialog).getAllByRole("row")).toHaveLength(4);
      await closeOnce(user, dialog);
    });

    it("still dispatches an outside drop once when the document prevents its browser default", async () => {
      const user = userEvent.setup();
      await openFromWindow(kind);
      let defaultPreventedAtDocument = false;
      const observe = (event: Event) => {
        event.preventDefault();
        defaultPreventedAtDocument = event.defaultPrevented;
      };
      document.addEventListener("drop", observe);
      try {
        // Preventing document navigation does not claim this outside upload.
        dropFiles(screen.getByTestId("page-drop-target"), [subtitle(2)]);
        expect(defaultPreventedAtDocument).toBe(true);
        expect(screen.getByTestId("modal-count")).toHaveTextContent("2");
        await user.click(
          nativeControl<HTMLButtonElement>(
            screen.getByRole("dialog"),
            ".mantine-Modal-close",
          ),
        );
        await waitFor(() =>
          expect(screen.getByTestId("modal-count")).toHaveTextContent("1"),
        );
        await closeOnce(user, screen.getByRole("dialog"));
      } finally {
        document.removeEventListener("drop", observe);
      }
    });
  },
);
