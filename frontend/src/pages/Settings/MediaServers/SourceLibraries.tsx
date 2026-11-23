/* eslint-disable camelcase */

import { type FormEvent, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Code,
  Divider,
  Group,
  Modal,
  NativeSelect,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useLanguageProfiles } from "@/apis/hooks/languages";
import {
  mediaSourceErrorMessage,
  useMediaSources,
  usePreviewServerSourceRoots,
  useScanMediaSource,
  useServerSourceLibraryChoices,
  useValidateServerSourceRoots,
  useWriteMediaSource,
} from "@/apis/hooks/mediaSources";
import api from "@/apis/raw";
import type { MediaServerInstance } from "@/apis/raw/mediaServers";
import type {
  MediaSource,
  ServerRootMapping,
  ServerSourceKind,
  SourceContentKind,
  SourceLibrary,
} from "@/apis/raw/mediaSources";
import { CONTENT_KIND_LABELS } from "@/pages/Settings/Connections/LibraryRows";

interface Props {
  instance: MediaServerInstance;
  kind: ServerSourceKind;
}

type Editor =
  | { mode: "create" }
  | { mode: "add"; source: MediaSource }
  | { mode: "roots"; source: MediaSource; library: SourceLibrary };

const emptyRoot = () => ({ server_path: "", local_path: "" });

function pathError(path: string, server: boolean) {
  if (!path) return undefined;
  if (
    !/^(\/|[a-z]:[/\\]|\\\\[^\\]+\\[^\\]+)/i.test(path) ||
    path.includes("\0")
  ) {
    return "Enter an absolute path.";
  }
  if (server && path.split(/[/\\]/).includes("..")) {
    return "Enter an absolute server path without parent segments.";
  }
  return undefined;
}

function SourceLibraryEditor({
  editor,
  instance,
  kind,
  onClose,
}: Props & { editor: Editor; onClose: () => void }) {
  const library = editor.mode === "roots" ? editor.library : undefined;
  const discovery = useServerSourceLibraryChoices(instance.id, !library);
  const { data: profiles } = useLanguageProfiles();
  const save = useWriteMediaSource();
  const preview = usePreviewServerSourceRoots();
  const [name, setName] = useState(instance.name);
  const [selection, setSelection] = useState("");
  const [content, setContent] = useState<SourceContentKind | "">("");
  const [profile, setProfile] = useState("");
  const [roots, setRoots] = useState<ServerRootMapping[]>(
    library?.roots?.map(({ server_path, local_path }) => ({
      server_path,
      local_path,
    })) ?? [],
  );
  const [confirmedCount, setConfirmedCount] = useState<number>();
  const editingRoots = editor.mode === "roots";
  const pending = save.isPending || preview.isPending;
  const source = "source" in editor ? editor.source : undefined;
  const choices = (discovery.data?.data ?? []).filter(
    ({ id }) => !source?.libraries.some(({ locator }) => locator === id),
  );
  const currentChoice = choices.find(({ id }) => id === selection);
  const checks = roots.map((root) => ({
    server: pathError(root.server_path, true),
    local: pathError(root.local_path, false),
  }));
  const rootsValid =
    roots.length > 0 &&
    roots.every(
      ({ server_path, local_path }, index) =>
        server_path.trim() &&
        local_path.trim() &&
        !checks[index].server &&
        !checks[index].local,
    );
  const [debouncedRoots] = useDebouncedValue(roots, 300);
  const currentRoots = roots === debouncedRoots;
  const validationKind = library?.content_kind || content;
  const validateRoots =
    rootsValid && !!validationKind && (editingRoots || !!currentChoice);
  const validation = useValidateServerSourceRoots(
    {
      instanceId: instance.id,
      locator: library?.locator ?? selection,
      sourceId: source?.id,
      libraryId: library?.id,
      roots: debouncedRoots,
      contentKind: validationKind || "movie",
    },
    validateRoots && currentRoots,
  );
  const checkingRoots =
    validateRoots &&
    (!currentRoots || validation.isPending || validation.isFetching);
  const validationError =
    validateRoots && currentRoots && !checkingRoots && validation.isError;
  const canSubmit =
    validateRoots &&
    currentRoots &&
    validation.isSuccess &&
    !checkingRoots &&
    (editingRoots ||
      (!discovery.isFetching &&
        !discovery.isError &&
        !!currentChoice &&
        !!content &&
        (editor.mode !== "create" || !!name.trim())));
  const clearFeedback = () => {
    preview.reset();
    save.reset();
  };
  const changeRoot = (
    index: number,
    field: keyof ServerRootMapping,
    value: string,
  ) => {
    setRoots((rows) =>
      rows.map((row, rowIndex) =>
        index === rowIndex ? { ...row, [field]: value } : row,
      ),
    );
    clearFeedback();
  };
  const selectLibrary = (id: string) => {
    const choice = choices.find((item) => item.id === id);
    setSelection(id);
    setContent(choice?.content_kind ?? "");
    setRoots(
      choice?.roots.length
        ? choice.roots.map(({ server_path, local_path }) => ({
            server_path,
            local_path: local_path ?? "",
          }))
        : [emptyRoot()],
    );
    clearFeedback();
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (pending || !canSubmit) return;
    if (editor.mode === "roots") {
      save.reset();
      preview.mutate(
        { sourceId: editor.source.id, libraryId: editor.library.id, roots },
        { onSuccess: setConfirmedCount },
      );
    } else if (content) {
      const draft = {
        server_library_id: selection,
        content_kind: content,
        profile_id: profile ? Number(profile) : null,
        roots,
      };
      save.mutate(
        () =>
          editor.mode === "create"
            ? api.mediaSources.createServerSource(
                kind,
                instance.id,
                name.trim(),
                draft,
              )
            : api.mediaSources.addServerLibrary(editor.source.id, draft),
        { onSuccess: onClose },
      );
    }
  };
  const confirm = () => {
    if (editor.mode !== "roots" || confirmedCount === undefined || pending)
      return;
    save.mutate(
      () =>
        api.mediaSources.updateServerRoots(
          editor.source.id,
          editor.library.id,
          roots,
          confirmedCount,
        ),
      { onSuccess: onClose, onError: () => setConfirmedCount(undefined) },
    );
  };
  const confirming = confirmedCount !== undefined;
  const dismiss = () => {
    if (pending) return;
    if (confirming) setConfirmedCount(undefined);
    else onClose();
  };
  const title = confirming
    ? "Confirm root changes"
    : editingRoots
      ? "Edit library roots"
      : editor.mode === "create"
        ? "Use as a library source"
        : "Add server library";

  return (
    <Modal
      opened
      onClose={dismiss}
      title={title}
      centered
      size="lg"
      closeOnClickOutside={!pending}
      closeOnEscape={!pending}
      withCloseButton={!pending}
    >
      {confirming ? (
        <Stack gap="md">
          <Text size="sm">
            These root changes will remove {confirmedCount} indexed{" "}
            {confirmedCount === 1 ? "item" : "items"} from this library.
          </Text>
          <Text size="sm">
            Subtitle history, blacklist entries, and files on disk are kept.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={dismiss} disabled={pending}>
              Back
            </Button>
            <Button
              color={confirmedCount ? "red" : undefined}
              onClick={confirm}
              loading={save.isPending}
            >
              Apply root changes
            </Button>
          </Group>
        </Stack>
      ) : (
        <form onSubmit={submit}>
          <Stack gap="md">
            {editor.mode === "create" && (
              <TextInput
                label="Source name"
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
                required
                disabled={pending}
              />
            )}
            {!editingRoots && (
              <>
                {discovery.isFetching && (
                  <Text role="status" size="sm">
                    Loading server libraries…
                  </Text>
                )}
                {discovery.isError ? (
                  <Alert
                    role="alert"
                    color="red"
                    title="Could not load server libraries"
                  >
                    <Stack align="flex-start" gap="xs">
                      <Text size="sm">
                        {mediaSourceErrorMessage(
                          discovery.error,
                          "Check the saved connection and try again.",
                        )}
                      </Text>
                      <Button
                        variant="light"
                        size="xs"
                        onClick={() => void discovery.refetch()}
                      >
                        Retry
                      </Button>
                    </Stack>
                  </Alert>
                ) : (
                  <NativeSelect
                    label="Server library"
                    required
                    value={selection}
                    disabled={
                      pending || discovery.isFetching || !choices.length
                    }
                    onChange={(event) =>
                      selectLibrary(event.currentTarget.value)
                    }
                    data={[
                      {
                        value: "",
                        label:
                          discovery.isSuccess && !choices.length
                            ? "No libraries available to add"
                            : "Select a library",
                        disabled: true,
                      },
                      ...choices.map(({ id, name }) => ({
                        value: id,
                        label: name,
                      })),
                    ]}
                  />
                )}
                {!!currentChoice && (
                  <>
                    <NativeSelect
                      label="Content kind"
                      required
                      value={content}
                      disabled={pending}
                      onChange={(event) => {
                        const value = event.currentTarget.value;
                        if (
                          value === "movie" ||
                          value === "series" ||
                          value === "sports"
                        ) {
                          setContent(value);
                          clearFeedback();
                        }
                      }}
                      data={[
                        {
                          value: "",
                          label: "Choose a content kind",
                          disabled: true,
                        },
                        ...Object.entries(CONTENT_KIND_LABELS).map(
                          ([value, label]) => ({ value, label }),
                        ),
                      ]}
                    />
                    <NativeSelect
                      label="Language profile"
                      value={profile}
                      disabled={pending}
                      onChange={(event) =>
                        setProfile(event.currentTarget.value)
                      }
                      data={[
                        { value: "", label: "No profile" },
                        ...(profiles ?? []).map(({ profileId, name }) => ({
                          value: String(profileId),
                          label: name,
                        })),
                      ]}
                    />
                  </>
                )}
              </>
            )}
            {(editingRoots || currentChoice) && (
              <Stack gap="sm">
                <Divider label="Library roots" />
                <Text size="sm" c="dimmed">
                  Map each server path to the absolute folder path Bazarr+ uses
                  to access the same files. In Docker, use the path inside the
                  Bazarr+ container.
                </Text>
                {roots.map((root, index) => (
                  <Group key={index} grow align="flex-start">
                    <TextInput
                      label="Server path"
                      aria-label={`Server path ${index + 1}`}
                      required
                      disabled={pending}
                      error={checks[index].server}
                      value={root.server_path}
                      onChange={(event) =>
                        changeRoot(
                          index,
                          "server_path",
                          event.currentTarget.value,
                        )
                      }
                    />
                    <TextInput
                      label="Local path"
                      aria-label={`Local path ${index + 1}`}
                      required
                      disabled={pending}
                      error={checks[index].local}
                      value={root.local_path}
                      onChange={(event) =>
                        changeRoot(
                          index,
                          "local_path",
                          event.currentTarget.value,
                        )
                      }
                    />
                    {roots.length > 1 && (
                      <Button
                        type="button"
                        size="xs"
                        variant="subtle"
                        color="red"
                        mt={25}
                        aria-label={`Remove root ${index + 1}`}
                        disabled={pending}
                        onClick={() => {
                          setRoots(roots.filter((_, row) => row !== index));
                          clearFeedback();
                        }}
                      >
                        Remove
                      </Button>
                    )}
                  </Group>
                ))}
                <Group>
                  <Button
                    type="button"
                    size="xs"
                    variant="light"
                    disabled={pending}
                    onClick={() => setRoots([...roots, emptyRoot()])}
                  >
                    Add root
                  </Button>
                </Group>
                {checkingRoots && (
                  <Text role="status" size="sm" c="dimmed">
                    Checking library roots…
                  </Text>
                )}
                {validationError && (
                  <Alert
                    role="alert"
                    color="red"
                    title="Could not validate library roots"
                  >
                    <Stack align="flex-start" gap="xs">
                      <Text size="sm">
                        {mediaSourceErrorMessage(
                          validation.error,
                          "Check the paths and try again.",
                        )}
                      </Text>
                      <Button
                        type="button"
                        variant="light"
                        size="xs"
                        onClick={() => void validation.refetch()}
                      >
                        Retry
                      </Button>
                    </Stack>
                  </Alert>
                )}
              </Stack>
            )}
            {(preview.isError || save.isError) && (
              <Alert
                role="alert"
                color="red"
                title={
                  preview.isError ? "Could not preview roots" : "Could not save"
                }
              >
                {mediaSourceErrorMessage(
                  preview.isError ? preview.error : save.error,
                  "Check the paths and try again.",
                )}
              </Alert>
            )}
            <Group justify="flex-end">
              <Button variant="default" onClick={dismiss} disabled={pending}>
                Cancel
              </Button>
              <Button type="submit" loading={pending} disabled={!canSubmit}>
                {editingRoots
                  ? "Preview changes"
                  : editor.mode === "create"
                    ? "Add source"
                    : "Add library"}
              </Button>
            </Group>
          </Stack>
        </form>
      )}
    </Modal>
  );
}

export default function SourceLibraries({ instance, kind }: Props) {
  const query = useMediaSources();
  const scan = useScanMediaSource();
  const [editor, setEditor] = useState<Editor | null>(null);
  if (!query.isEnabled) return null;
  const source = query.data?.data.find(
    (row) => row.kind === kind && row.media_server_instance_id === instance.id,
  );
  const available = instance.enabled && instance.api_key_set;
  return (
    <Stack gap="sm">
      <Divider label="Library source" />
      {query.isLoading ? (
        <Text role="status" size="sm">
          Loading library source…
        </Text>
      ) : query.isError ? (
        <Alert role="alert" color="red">
          <Group justify="space-between">
            <Text size="sm">Could not load library source.</Text>
            <Button
              size="xs"
              variant="light"
              onClick={() => void query.refetch()}
            >
              Retry
            </Button>
          </Group>
        </Alert>
      ) : source ? (
        <>
          <Group justify="space-between">
            <Group gap="xs">
              <Text fw={600} size="sm">
                {source.name}
              </Text>
              {!source.enabled && (
                <Badge size="xs" color="gray">
                  Disabled
                </Badge>
              )}
            </Group>
            <Group gap="xs">
              <Button
                size="xs"
                variant="light"
                disabled={!available}
                onClick={() => setEditor({ mode: "add", source })}
              >
                Add library
              </Button>
              <Button
                size="xs"
                variant="default"
                loading={scan.isPending}
                disabled={
                  !available ||
                  !source.enabled ||
                  !source.libraries.some((library) => library.enabled)
                }
                onClick={() => scan.mutate(source.id)}
              >
                Scan libraries
              </Button>
            </Group>
          </Group>
          {source.libraries.map((library) => (
            <Stack key={library.id} gap={4}>
              <Group justify="space-between">
                <Group gap="xs">
                  <Text size="sm" fw={500}>
                    {library.name || library.locator}
                  </Text>
                  <Badge size="xs" variant="light">
                    {CONTENT_KIND_LABELS[library.content_kind]}
                  </Badge>
                  {!library.enabled && (
                    <Badge size="xs" color="gray">
                      Disabled
                    </Badge>
                  )}
                </Group>
                <Button
                  size="xs"
                  variant="subtle"
                  onClick={() => setEditor({ mode: "roots", source, library })}
                >
                  Edit roots
                </Button>
              </Group>
              {library.roots?.map(({ server_path, local_path }) => (
                <Group key={server_path} gap="xs" wrap="wrap">
                  <Code style={{ overflowWrap: "anywhere" }}>
                    {server_path}
                  </Code>
                  <Text size="xs" c="dimmed">
                    maps to
                  </Text>
                  <Code style={{ overflowWrap: "anywhere" }}>{local_path}</Code>
                </Group>
              ))}
            </Stack>
          ))}
          {scan.isSuccess && (
            <Text role="status" size="sm">
              Library scan requested.
            </Text>
          )}
          {scan.isError && (
            <Alert role="alert" color="red" title="Could not scan libraries">
              {mediaSourceErrorMessage(scan.error, "Try again.")}
            </Alert>
          )}
        </>
      ) : (
        <Group justify="space-between">
          <Text size="sm" c="dimmed">
            {available
              ? "Index selected libraries for subtitle management."
              : "Enable the connection and save an API key to add libraries."}
          </Text>
          <Button
            size="xs"
            variant="light"
            disabled={!available}
            onClick={() => setEditor({ mode: "create" })}
          >
            Use as a library source
          </Button>
        </Group>
      )}
      {editor && (
        <SourceLibraryEditor
          instance={instance}
          kind={kind}
          editor={editor}
          onClose={() => setEditor(null)}
        />
      )}
    </Stack>
  );
}
