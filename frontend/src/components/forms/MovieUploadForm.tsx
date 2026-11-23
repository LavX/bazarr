import React, {
  FunctionComponent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  Button,
  Checkbox,
  Divider,
  Group,
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
import { useMovieSubtitleModification } from "@/apis/hooks";
import api from "@/apis/raw";
import { subtitlesTypeOptions } from "@/components/forms/uploadFormSelectorTypes";
import { shouldAutoCloseUpload } from "@/components/forms/uploadHelpers";
import {
  Action,
  DropContent,
  FullPageDropzone,
  Selector,
} from "@/components/inputs";
import SimpleTable from "@/components/tables/SimpleTable";
import TextPopover from "@/components/TextPopover";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/notification";
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
  validateResult?: SubtitleValidateResult;
};

type SubtitleValidateResult = {
  state: "valid" | "warning" | "error";
  messages?: string;
};

const validator = (
  subtitles: readonly Subtitle[],
  file: SubtitleFile,
): SubtitleValidateResult => {
  if (file.language === null) {
    return {
      state: "error",
      messages: "Language is not selected",
    };
  } else {
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

interface BatchProps {
  files: File[];
  subtitles: readonly Subtitle[];
  languages: Language.Info[];
  defaultLanguage: Language.Info | null;
  upload: (form: FormType.UploadSubtitle) => void | Promise<unknown>;
  capturePageDrops?: boolean;
  independentFlags?: boolean;
  error?: React.ReactNode;
  onComplete?: () => void;
}

export const SubtitleBatchUploadForm: FunctionComponent<BatchProps> = ({
  files,
  subtitles,
  languages,
  defaultLanguage,
  upload,
  capturePageDrops = false,
  independentFlags = false,
  error,
  onComplete,
}) => {
  const modals = useModals();
  const languageOptions = useSelectorOptions(
    uniqBy(languages, "code2"),
    (v) => v.name,
    (v) => v.code2,
  );

  const buildRow = useCallback(
    (file: File): SubtitleFile => {
      const row: SubtitleFile = {
        file,
        language: defaultLanguage,
        forced: defaultLanguage?.forced ?? false,
        hi: defaultLanguage?.hi ?? false,
      };
      return { ...row, validateResult: validator(subtitles, row) };
    },
    [defaultLanguage, subtitles],
  );

  const [processing, setProcessing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [ready, setReady] = useState(false);

  const form = useForm({
    initialValues: {
      // Archives are expanded asynchronously on mount; start with the plain
      // subtitle files so the common case renders instantly.
      files: files.filter((file) => !isArchiveFile(file)).map(buildRow),
    },
    validate: {
      files: FormUtils.validation((values: SubtitleFile[]) => {
        return (
          values.find(
            (v: SubtitleFile) =>
              v.language === null ||
              v.validateResult === undefined ||
              v.validateResult.state === "error",
          ) === undefined
        );
      }, "Some files cannot be uploaded, please check"),
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

  // Expand any archives in the initially-provided files once on mount, then
  // arm the empty-close guard below.
  useEffect(() => {
    let cancelled = false;
    const archives = files.filter(isArchiveFile);
    void (async () => {
      if (archives.length > 0) {
        await addFiles(archives);
      }
      if (!cancelled) {
        setReady(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // Only the initially-provided files matter here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (shouldAutoCloseUpload(ready, processing, form.values.files.length)) {
      modals.closeSelf();
    }
  }, [ready, processing, form.values.files.length, modals]);

  const action = useArrayAction<SubtitleFile>((fn) => {
    form.setValues((values) => {
      const newFiles = fn(values.files ?? []);
      newFiles.forEach((v) => {
        v.validateResult = validator(subtitles, v);
      });

      return { ...values, files: newFiles };
    });
  });

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
          <FontAwesomeIcon icon={icon} />
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
            original: { file },
          },
        }) => {
          return <Text className="table-primary">{file.name}</Text>;
        },
      },
      {
        header: "Language",
        accessorKey: "language",
        cell: ({ row: { original, index } }) => {
          return (
            <Selector
              {...languageOptions}
              aria-label="Language"
              className="table-long-break"
              disabled={submitting}
              value={original.language}
              onChange={(item) => {
                action.mutate(index, { ...original, language: item });
              }}
            ></Selector>
          );
        },
      },
      {
        header: independentFlags
          ? "Type"
          : () => (
              <Selector
                options={subtitlesTypeOptions}
                value={null}
                disabled={submitting}
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
          if (independentFlags) {
            return (
              <Group wrap="nowrap">
                <Checkbox
                  label="Hearing impaired"
                  checked={original.hi}
                  disabled={submitting}
                  onChange={(event) =>
                    action.mutate(index, {
                      ...original,
                      hi: event.currentTarget.checked,
                    })
                  }
                />
                <Checkbox
                  label="Forced"
                  checked={original.forced}
                  disabled={submitting}
                  onChange={(event) =>
                    action.mutate(index, {
                      ...original,
                      forced: event.currentTarget.checked,
                    })
                  }
                />
              </Group>
            );
          }
          return (
            <Select
              disabled={submitting}
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
        id: "action",
        cell: ({ row: { index } }) => {
          return (
            <Action
              label="Remove"
              icon={faTrash}
              c="red"
              disabled={submitting}
              onClick={() => action.remove(index)}
            ></Action>
          );
        },
      },
    ],
    [action, independentFlags, languageOptions, submitting],
  );

  return (
    <form
      onSubmit={form.onSubmit(async ({ files }) => {
        if (processing || submitting) return;
        setSubmitting(true);
        let completed = 0;
        try {
          for (const { file, language, hi, forced } of files) {
            if (language === null) {
              throw new Error("Language is not selected");
            }
            const result = upload({
              file,
              language: language.code2,
              hi,
              forced,
            });
            if (result) await result;
            completed += 1;
          }
        } catch {
          // Keep the failed and unattempted files with their selected metadata.
          form.setFieldValue("files", files.slice(completed));
          return;
        } finally {
          setSubmitting(false);
        }

        onComplete?.();
        modals.closeSelf();
      })}
    >
      <Stack className="table-long-break">
        {error}
        {capturePageDrops && (
          <FullPageDropzone
            active
            onDrop={(dropped) => {
              if (!processing && !submitting) void addFiles(dropped);
            }}
          />
        )}
        <Dropzone
          data-full-page-dropzone-ignore
          onDrop={(dropped) => void addFiles(dropped)}
          loading={processing || submitting}
          multiple
        >
          <DropContent></DropContent>
        </Dropzone>
        <SimpleTable columns={columns} data={form.values.files}></SimpleTable>
        <Divider></Divider>
        <Button
          type="submit"
          loading={processing || submitting}
          disabled={processing || submitting || form.values.files.length === 0}
        >
          Upload
        </Button>
      </Stack>
    </form>
  );
};

interface Props {
  files: File[];
  movie: Item.Movie;
  onComplete?: () => void;
}

const MovieUploadForm: FunctionComponent<Props> = ({
  files,
  movie,
  onComplete,
}) => {
  const profile = useLanguageProfileBy(movie.profileId);
  const languages = useProfileItemsToLanguages(profile);
  const { upload } = useMovieSubtitleModification();
  return (
    <SubtitleBatchUploadForm
      files={files}
      subtitles={movie.subtitles}
      languages={languages}
      defaultLanguage={languages[0] ?? null}
      upload={(form) =>
        upload.mutate({
          radarrId: movie.radarrId,
          arrInstanceId: movie.arr_instance_id,
          form,
        })
      }
      onComplete={onComplete}
    />
  );
};

export const MovieUploadModal = withModal(
  MovieUploadForm,
  "upload-movie-subtitle",
  {
    title: "Upload Subtitles",
    size: "xl",
  },
);

export default MovieUploadForm;
