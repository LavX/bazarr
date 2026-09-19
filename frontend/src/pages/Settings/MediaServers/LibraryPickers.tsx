/* eslint-disable camelcase */

import { Alert, Button, Group, MultiSelect, Stack, Text } from "@mantine/core";
import type {
  MediaServerKind,
  MediaServerLibrary,
  MediaServerOptions,
} from "@/apis/raw/mediaServers";
import { LIBRARY_OPTION_KEYS } from "@/apis/raw/mediaServers";
import { kindName } from "./kinds";

type LibraryQuery = {
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  isIdle: boolean;
  data?: MediaServerLibrary[];
  mutate: () => void;
};

const SECTIONS = [
  {
    slot: "movie" as const,
    label: "Movie libraries",
    types: ["movies", "movie"],
  },
  {
    slot: "series" as const,
    label: "Series libraries",
    types: ["tvshows", "series", "show"],
  },
  // A sports root lives inside a library of some other type, so nothing can be
  // filtered out of this one.
  { slot: "sports" as const, label: "Sports libraries", types: null },
];

// A handle the user chose but the server did not list must stay selectable:
// a library it could not reach is not a library the user un-chose.
function options(available: MediaServerLibrary[], selected: string[]) {
  const listed = new Map(
    available.map((library) => [library.id, library.name || library.id]),
  );
  for (const value of selected)
    if (!listed.has(value)) listed.set(value, value);
  return [...listed].map(([value, label]) => ({ value, label }));
}

export default function LibraryPickers({
  kind,
  value,
  onChange,
  libraries,
  configured,
}: {
  kind: "jellyfin" | "plex";
  value: MediaServerOptions;
  onChange: (options: MediaServerOptions) => void;
  libraries: LibraryQuery;
  configured: boolean;
}) {
  const name = kindName(kind as MediaServerKind);
  const keys = LIBRARY_OPTION_KEYS[kind];
  const available = libraries.data ?? [];
  return (
    <Stack gap="md">
      <Group>
        <Button
          type="button"
          variant="light"
          disabled={!configured}
          loading={libraries.isPending}
          onClick={() => libraries.mutate()}
        >
          Load libraries
        </Button>
      </Group>
      {libraries.isError && (
        <Alert color="red">
          Could not load {name} libraries. Check the connection and library
          access. Saved library choices are preserved.
        </Alert>
      )}
      {libraries.isSuccess && available.length === 0 && (
        <Alert color="gray">
          No libraries found. Check that the credential has access to them.
        </Alert>
      )}
      {libraries.isIdle && (
        <Text size="sm" c="dimmed">
          Load libraries to choose which ones this instance refreshes.
        </Text>
      )}
      {SECTIONS.map(({ slot, label, types }) => {
        const key = keys[slot];
        const selected = value[key as keyof MediaServerOptions];
        const current = Array.isArray(selected) ? (selected as string[]) : [];
        const listed = types
          ? available.filter((library) => types.includes(library.type))
          : available;
        return (
          <MultiSelect
            key={key}
            label={label}
            placeholder={current.length ? undefined : "None selected"}
            data={options(listed, current)}
            value={current}
            onChange={(next) => onChange({ ...value, [key]: next })}
          />
        );
      })}
      <Text size="sm" c="dimmed">
        A refresh that cannot resolve the item falls back to scanning these
        libraries. An instance with none configured is left alone.
      </Text>
    </Stack>
  );
}
