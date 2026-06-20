import React, {
  FunctionComponent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  Button,
  Divider,
  MantineColor,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { Dropzone } from "@mantine/dropzone";
import { useForm } from "@mantine/form";
import { showNotification } from "@mantine/notifications";
import {
  faCheck,
  faCircleNotch,
  faInfoCircle,
  faTimes,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { isString, uniqBy } from "lodash";
import {
  useEpisodesBySeriesId,
  useEpisodeSubtitleModification,
  useSubtitleInfos,
} from "@/apis/hooks";
import api from "@/apis/raw";
import { subtitlesTypeOptions } from "@/components/forms/uploadFormSelectorTypes";
import { matchEpisode } from "@/components/forms/uploadHelpers";
import { Action, DropContent, Selector } from "@/components/inputs";
import SimpleTable from "@/components/tables/SimpleTable";
import TextPopover from "@/components/TextPopover";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/task";
import { useArrayAction, useSelectorOptions } from "@/utilities";
import { expandArchives, isArchiveFile } from "@/utilities/archives";
import FormUtils from "@/utilities/form";
import {
  useLanguageProfileBy,
  useProfileItemsToLanguages,
} from "@/utilities/languages";

type SubtitleFile = {
  file: File;
  language: Language.Info | null;
  forced: boolean;
  hi: boolean;
  episode: Item.Episode | null;
  validateResult?: SubtitleValidateResult;
};

type SubtitleValidateResult = {
  state: "valid" | "warning" | "error";
  messages?: string;
};

const validator = (file: SubtitleFile): SubtitleValidateResult => {
  if (file.language === null) {
    return {
      state: "error",
      messages: "Language is not selected",
    };
  } else if (file.episode === null) {
    return {
      state: "error",
      messages: "Episode is not selected",
    };
  } else {
    const { subtitles } = file.episode;
    const existing = subtitles.find(
      (v) => v.code2 === file.language?.code2 && isString(v.path),
    );
    if (existing !== undefined) {
      return {
        state: "warning",
        messages: "Override existing subtitle",
      };
    }
  }

  return {
    state: "valid",
  };
};

interface Props {
  files: File[];
  series: Item.Series;
  onComplete?: VoidFunction;
}

const SeriesUploadForm: FunctionComponent<Props> = ({
  series,
  files,
  onComplete,
}) => {
  const modals = useModals();
  const episodes = useEpisodesBySeriesId(series.sonarrSeriesId);
  const episodeOptions = useSelectorOptions(
    episodes.data ?? [],
    (v) => `(${v.season}x${v.episode}) ${v.title}`,
    (v) => v.sonarrEpisodeId.toString(),
  );

  const profile = useLanguageProfileBy(series.profileId);
  const languages = useProfileItemsToLanguages(profile);
  const languageOptions = useSelectorOptions(
    uniqBy(languages, "code2"),
    (v) => v.name,
    (v) => v.code2,
  );

  const defaultLanguage = useMemo(
    () => (languages.length > 0 ? languages[0] : null),
    [languages],
  );

  const buildRow = useCallback(
    (file: File): SubtitleFile => {
      const row: SubtitleFile = {
        file,
        language: defaultLanguage,
        forced: defaultLanguage?.forced ?? false,
        hi: defaultLanguage?.hi ?? false,
        episode: null,
      };
      return { ...row, validateResult: validator(row) };
    },
    [defaultLanguage],
  );

  const [processing, setProcessing] = useState(false);

  const form = useForm({
    initialValues: {
      // Archives are expanded asynchronously on mount; start with the plain
      // subtitle files so the common case renders instantly.
      files: files.filter((file) => !isArchiveFile(file)).map(buildRow),
    },
    validate: {
      files: FormUtils.validation(
        (values: SubtitleFile[]) =>
          values.find(
            (v: SubtitleFile) =>
              v.language === null ||
              v.episode === null ||
              v.validateResult === undefined ||
              v.validateResult.state === "error",
          ) === undefined,
        "Some files cannot be uploaded, please check",
      ),
    },
  });

  // Add dropped/selected files: archives (#233) are extracted on the backend
  // and replaced by their subtitle entries; plain files pass through.
  const addFiles = useCallback(
    async (incoming: File[]) => {
      if (incoming.length === 0) {
        return;
      }
      if (incoming.some(isArchiveFile)) {
        setProcessing(true);
      }
      const { files: expanded, errors } = await expandArchives(
        incoming,
        (file) => api.subtitles.extractArchive(file),
      );
      if (errors.length > 0) {
        showNotification(
          notification.warn(
            "Could not extract some archives",
            errors.join(", "),
          ),
        );
      }
      if (expanded.length > 0) {
        form.setValues((values) => ({
          ...values,
          files: [...(values.files ?? []), ...expanded.map(buildRow)],
        }));
      }
      setProcessing(false);
    },
    [form, buildRow],
  );

  // Expand any archives in the initially-provided files once on mount.
  useEffect(() => {
    const archives = files.filter(isArchiveFile);
    if (archives.length > 0) {
      void addFiles(archives);
    }
    // Only the initially-provided files matter here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const action = useArrayAction<SubtitleFile>((fn) => {
    form.setValues((values) => {
      const newFiles = fn(values.files ?? []);
      newFiles.forEach((v) => {
        v.validateResult = validator(v);
      });
      return { ...values, files: newFiles };
    });
  });

  // Derived from the live rows (not just the initial prop) so files added via
  // the dropzone or extracted from an archive also get episode auto-matched.
  const names = useMemo(
    () => form.values.files.map((v) => v.file.name),
    [form.values.files],
  );
  const infos = useSubtitleInfos(names);

  // Auto assign episode if available. Preserve a row that already has an
  // episode (manually chosen, or matched earlier) so adding more files does not
  // clobber the user's selection when this effect re-runs.
  useEffect(() => {
    if (infos.data !== undefined) {
      const infoList = infos.data;
      const episodeList = episodes.data ?? [];
      action.update((item) => {
        if (item.episode) {
          return item;
        }
        const matched = matchEpisode(item.file.name, infoList, episodeList);
        return matched ? { ...item, episode: matched } : item;
      });
    }
  }, [action, episodes.data, infos.data]);

  const ValidateResultCell = ({
    validateResult,
  }: {
    validateResult: SubtitleValidateResult | undefined;
  }) => {
    const icon = useMemo(() => {
      switch (validateResult?.state) {
        case "valid":
          return faCheck;
        case "warning":
          return faInfoCircle;
        case "error":
          return faTimes;
        default:
          return faCircleNotch;
      }
    }, [validateResult?.state]);

    const color = useMemo<MantineColor | undefined>(() => {
      switch (validateResult?.state) {
        case "valid":
          return "green";
        case "warning":
          return "yellow";
        case "error":
          return "red";
        default:
          return undefined;
      }
    }, [validateResult?.state]);

    return (
      <TextPopover text={validateResult?.messages}>
        <Text c={color} inline>
          <FontAwesomeIcon icon={icon}></FontAwesomeIcon>
        </Text>
      </TextPopover>
    );
  };

  const columns = useMemo<ColumnDef<SubtitleFile>[]>(
    () => [
      {
        id: "validateResult",
        cell: ({
          row: {
            original: { validateResult },
          },
        }) => {
          return <ValidateResultCell validateResult={validateResult} />;
        },
      },
      {
        header: "File",
        id: "filename",
        accessorKey: "file",
        cell: ({
          row: {
            original: {
              file: { name },
            },
          },
        }) => {
          return <Text className="table-primary">{name}</Text>;
        },
      },
      {
        header: () => (
          <Selector
            {...languageOptions}
            value={null}
            placeholder="Language"
            onChange={(value) => {
              if (value) {
                action.update((item) => {
                  return { ...item, language: value };
                });
              }
            }}
          ></Selector>
        ),
        accessorKey: "language",
        cell: ({ row: { original, index } }) => {
          return (
            <Selector
              {...languageOptions}
              className="table-select"
              value={original.language}
              onChange={(item) => {
                action.mutate(index, { ...original, language: item });
              }}
            ></Selector>
          );
        },
      },
      {
        header: () => (
          <Selector
            options={subtitlesTypeOptions}
            value={null}
            placeholder="Type"
            onChange={(value) => {
              if (value) {
                action.update((item) => {
                  switch (value) {
                    case "hi":
                      return { ...item, hi: true, forced: false };
                    case "forced":
                      return { ...item, hi: false, forced: true };
                    case "normal":
                      return { ...item, hi: false, forced: false };
                    default:
                      return item;
                  }
                });
              }
            }}
          ></Selector>
        ),
        accessorKey: "type",
        cell: ({ row: { original, index } }) => {
          return (
            <Select
              value={
                subtitlesTypeOptions.find((s) => {
                  if (original.hi) {
                    return s.value === "hi";
                  }

                  if (original.forced) {
                    return s.value === "forced";
                  }

                  return s.value === "normal";
                })?.value
              }
              data={subtitlesTypeOptions}
              onChange={(value) => {
                if (value) {
                  action.mutate(index, {
                    ...original,
                    hi: value === "hi",
                    forced: value === "forced",
                  });
                }
              }}
            ></Select>
          );
        },
      },
      {
        id: "episode",
        header: "Episode",
        accessorKey: "episode",
        cell: ({ row: { original, index } }) => {
          return (
            <Selector
              {...episodeOptions}
              searchable
              className="table-select"
              value={original.episode}
              onChange={(item) => {
                action.mutate(index, { ...original, episode: item });
              }}
            ></Selector>
          );
        },
      },
      {
        id: "action",
        cell: ({ row: { index } }) => {
          return (
            <Action
              label="Remove"
              icon={faTrash}
              c="red"
              onClick={() => action.remove(index)}
            ></Action>
          );
        },
      },
    ],
    [action, episodeOptions, languageOptions],
  );

  const { upload } = useEpisodeSubtitleModification();

  return (
    <form
      onSubmit={form.onSubmit(({ files }) => {
        const { sonarrSeriesId: seriesId } = series;

        for (const value of files) {
          const { file, hi, forced, language, episode } = value;

          if (language === null || episode === null) {
            throw new Error(
              "Invalid language or episode. This shouldn't happen, please report this bug.",
            );
          }

          const { code2 } = language;
          const { sonarrEpisodeId: episodeId } = episode;

          upload.mutate({
            seriesId,
            episodeId,
            arrInstanceId: episode.arr_instance_id,
            form: {
              file,
              language: code2,
              hi,
              forced,
            },
          });
        }

        onComplete?.();
        modals.closeSelf();
      })}
    >
      <Stack className="table-long-break">
        <Dropzone
          onDrop={(dropped) => void addFiles(dropped)}
          loading={processing}
          multiple
        >
          <DropContent></DropContent>
        </Dropzone>
        <SimpleTable columns={columns} data={form.values.files}></SimpleTable>
        <Divider></Divider>
        <Button
          type="submit"
          loading={processing}
          disabled={processing || form.values.files.length === 0}
        >
          Upload
        </Button>
      </Stack>
    </form>
  );
};

export const SeriesUploadModal = withModal(
  SeriesUploadForm,
  "upload-series-subtitles",
  { title: "Upload Subtitles", size: "xl" },
);

export default SeriesUploadForm;
