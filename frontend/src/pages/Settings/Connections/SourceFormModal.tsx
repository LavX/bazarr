import { type FormEvent, useState } from "react";
import {
  Alert,
  Button,
  Group,
  List,
  Modal,
  NativeSelect,
  NumberInput,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { useLanguageProfiles } from "@/apis/hooks/languages";
import {
  mediaSourceErrorMessage,
  useValidateLibraryPath,
  useWriteMediaSource,
} from "@/apis/hooks/mediaSources";
import api from "@/apis/raw";
import type {
  MediaSource,
  SourceContentKind,
  SourceLibrary,
  SourceLibraryDraft,
} from "@/apis/raw/mediaSources";
import { CONTENT_KIND_LABELS } from "./LibraryRows";

export type SourceEditor =
  | { mode: "create-source" }
  | { mode: "edit-source" | "add-library"; source: MediaSource }
  | { mode: "edit-library"; source: MediaSource; library: SourceLibrary };

const EDITOR_TITLES = {
  "create-source": "Add folder source",
  "edit-source": "Edit folder source",
  "add-library": "Add library",
  "edit-library": "Edit library",
};
const MAX_SCAN_HOURS = 24 * 365;
const NO_PROFILE = "none";

export default function SourceFormModal({
  editor,
  onClose,
}: {
  editor: SourceEditor;
  onClose: () => void;
}) {
  const { mode } = editor;
  const source = "source" in editor ? editor.source : undefined;
  const library = "library" in editor ? editor.library : undefined;
  const showSource = mode === "create-source" || mode === "edit-source";
  const showLibrary = mode !== "edit-source";
  const newLibrary = mode === "create-source" || mode === "add-library";
  const { data: profiles } = useLanguageProfiles();
  const save = useWriteMediaSource();
  const [name, setName] = useState(source?.name ?? "");
  const [enabled, setEnabled] = useState(
    library?.enabled ?? source?.enabled ?? true,
  );
  const [frequency, setFrequency] = useState<number | string>(
    source?.scan_frequency_hours ?? 12,
  );
  const [path, setPath] = useState(library?.locator ?? "");
  const [kind, setKind] = useState<SourceContentKind>(
    library?.content_kind ?? "movie",
  );
  const [profile, setProfile] = useState<string | null>(
    library?.profile_id != null ? String(library.profile_id) : NO_PROFILE,
  );
  const [debouncedPath] = useDebouncedValue(path, 300);
  const pathPresent = newLibrary && path.trim().length > 0;
  const currentPath = pathPresent && path === debouncedPath;
  const validation = useValidateLibraryPath(debouncedPath, kind, currentPath);
  const checking = pathPresent && (!currentPath || validation.isFetching);
  const result = currentPath && !checking ? validation.data : undefined;
  const pathError = currentPath && !checking && validation.isError;
  const sourceValid =
    !showSource ||
    (name.trim() &&
      typeof frequency === "number" &&
      Number.isInteger(frequency) &&
      frequency >= 0 &&
      frequency <= MAX_SCAN_HOURS);
  const canSave = sourceValid && (!newLibrary || (result?.ok && !pathError));

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave || save.isPending) return;
    const profileId =
      profile === NO_PROFILE || !profile ? null : Number(profile);
    const draft: SourceLibraryDraft = {
      path,
      content_kind: kind,
      profile_id: profileId,
    };
    const settings = {
      name: name.trim(),
      enabled,
      scan_frequency_hours: Number(frequency),
    };
    const write = () => {
      switch (editor.mode) {
        case "create-source":
          return api.mediaSources.create({ ...settings, library: draft });
        case "edit-source":
          return api.mediaSources.update(editor.source.id, settings);
        case "add-library":
          return api.mediaSources.addLibrary(editor.source.id, draft);
        case "edit-library":
          return api.mediaSources.updateLibrary(
            editor.source.id,
            editor.library.id,
            {
              enabled,
              profile_id: profileId,
            },
          );
      }
    };
    save.mutate(write, { onSuccess: onClose });
  };

  return (
    <Modal
      opened
      onClose={save.isPending ? () => undefined : onClose}
      title={EDITOR_TITLES[mode]}
      closeOnClickOutside={!save.isPending}
      closeOnEscape={!save.isPending}
      withCloseButton={!save.isPending}
    >
      <form onSubmit={submit}>
        <Stack gap="md">
          {showSource && (
            <>
              <TextInput
                label="Source name"
                required
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
              />
              <Switch
                label="Source enabled"
                checked={enabled}
                onChange={(event) => setEnabled(event.currentTarget.checked)}
              />
              <NumberInput
                label="Scan interval (hours)"
                description="Enter 0 to scan manually only."
                required
                min={0}
                max={MAX_SCAN_HOURS}
                allowDecimal={false}
                allowNegative={false}
                value={frequency}
                onChange={setFrequency}
              />
            </>
          )}
          {showLibrary && (
            <>
              <NativeSelect
                label="Content kind"
                value={kind}
                disabled={!newLibrary}
                onChange={(event) => {
                  const value = event.currentTarget.value;
                  if (
                    value === "movie" ||
                    value === "series" ||
                    value === "sports"
                  )
                    setKind(value);
                }}
                data={[
                  { value: "movie", label: CONTENT_KIND_LABELS.movie },
                  { value: "series", label: CONTENT_KIND_LABELS.series },
                  { value: "sports", label: CONTENT_KIND_LABELS.sports },
                ]}
              />
              <TextInput
                label="Folder path"
                description={
                  newLibrary
                    ? "Use an absolute path as Bazarr+ sees it. For Docker, use the path inside the container."
                    : "To change the path or content kind, remove the library and add it again."
                }
                required={newLibrary}
                readOnly={!newLibrary}
                value={path}
                placeholder={newLibrary ? "/media/movies" : undefined}
                onChange={(event) => setPath(event.currentTarget.value)}
                error={
                  result?.errors.length ? (
                    <span role="alert">
                      {result.errors.map(({ message }) => message).join(" ")}
                    </span>
                  ) : undefined
                }
              />
              {checking && (
                <Text role="status" size="sm" c="dimmed">
                  Checking folder…
                </Text>
              )}
              {pathError && (
                <Alert
                  color="red"
                  role="alert"
                  title="Could not check the folder"
                >
                  <Stack align="flex-start" gap="xs">
                    {mediaSourceErrorMessage(
                      validation.error,
                      "Check that Bazarr+ is reachable and try again.",
                    )}
                    <Button
                      size="compact-xs"
                      variant="light"
                      onClick={() => void validation.refetch()}
                    >
                      Retry
                    </Button>
                  </Stack>
                </Alert>
              )}
              {!!result?.warnings.length && (
                <Alert role="status" color="yellow" title="Folder warnings">
                  <List size="sm">
                    {[...new Set(result.warnings)].map((warning) => (
                      <List.Item key={warning}>{warning}</List.Item>
                    ))}
                  </List>
                </Alert>
              )}
              <Select
                label="Language profile"
                value={profile}
                allowDeselect={false}
                onChange={setProfile}
                data={[
                  { value: NO_PROFILE, label: "No profile" },
                  ...(profiles ?? []).map(({ profileId, name }) => ({
                    value: String(profileId),
                    label: name,
                  })),
                ]}
              />
              {mode === "edit-library" && (
                <Switch
                  label="Library enabled"
                  checked={enabled}
                  onChange={(event) => setEnabled(event.currentTarget.checked)}
                />
              )}
            </>
          )}
          {save.isError && (
            <Alert role="alert" color="red" title="Could not save">
              {mediaSourceErrorMessage(save.error, "Try again.")}
            </Alert>
          )}
          <Group justify="flex-end" mt="xs">
            <Button
              variant="default"
              onClick={onClose}
              disabled={save.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" loading={save.isPending} disabled={!canSave}>
              {mode === "create-source"
                ? "Add source"
                : newLibrary
                  ? "Add library"
                  : "Save"}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
}
