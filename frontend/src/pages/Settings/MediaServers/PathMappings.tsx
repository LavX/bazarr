/* eslint-disable camelcase */

import { Button, Group, Select, Stack, Text, TextInput } from "@mantine/core";
import type {
  MediaServerKind,
  PathMapping,
  SiloLibrary,
} from "@/apis/raw/mediaServers";

interface Props {
  kind: MediaServerKind;
  libraries?: SiloLibrary[];
  value: PathMapping[];
  onChange: (value: PathMapping[]) => void;
}

export default function PathMappings({
  kind,
  libraries = [],
  value: mappings,
  onChange: update,
}: Props) {
  const isSilo = kind === "silo";
  const options = libraries.map((library) => ({
    value: library.id,
    label: `${library.name} (${library.type})`,
  }));
  const change = (index: number, patch: Partial<PathMapping>) => {
    update(
      mappings.map((mapping, i) =>
        i === index ? { ...mapping, ...patch } : mapping,
      ),
    );
  };

  return (
    <Stack gap="md">
      <Text size="sm" c="dimmed">
        Local path is the folder as Bazarr sees it. Server path is the same
        folder as {isSilo ? "Silo" : "Emby"} sees it. Add a mapping for each
        media folder, even when both paths are the same. Mappings select which
        local videos notify this instance. A path matching several instances
        refreshes each one.
        {isSilo && " Select the Silo library that contains that server folder."}
      </Text>
      {mappings.map((mapping, index) => {
        const library = libraries.find(
          (item) => item.id === mapping.library_id,
        );
        const unavailable = isSilo && !library;
        const selectOptions =
          unavailable && mapping.library_id
            ? [
                ...options,
                {
                  value: mapping.library_id,
                  label: `Unavailable library (${mapping.library_id})`,
                  disabled: true,
                },
              ]
            : options;
        return (
          <Stack key={index} gap="xs">
            <Group align="flex-start" grow>
              <TextInput
                label={`Local path ${index + 1}`}
                value={mapping.local_path}
                placeholder="/tv"
                disabled={unavailable}
                onChange={(event) =>
                  change(index, { local_path: event.currentTarget.value })
                }
              />
              <TextInput
                label={`Server path ${index + 1}`}
                value={mapping.remote_path}
                placeholder="/media/tv"
                disabled={unavailable}
                onChange={(event) =>
                  change(index, { remote_path: event.currentTarget.value })
                }
              />
            </Group>
            {isSilo && (
              <>
                <Select
                  label={`Silo library ${index + 1}`}
                  data={selectOptions}
                  value={mapping.library_id ?? null}
                  searchable
                  allowDeselect={false}
                  disabled={libraries.length === 0}
                  onChange={(id) => {
                    if (
                      id !== null &&
                      libraries.some((item) => item.id === id)
                    ) {
                      change(index, { library_id: id });
                    }
                  }}
                />
                {unavailable && (
                  <Text size="xs" c="dimmed">
                    Load libraries and select an available library before
                    editing this mapping. The saved mapping is preserved until
                    you edit or remove it.
                  </Text>
                )}
                {library && (
                  <Text size="xs" c="dimmed">
                    Library folders:{" "}
                    {library.paths.join(", ") || "None reported"}
                  </Text>
                )}
              </>
            )}
            <Group justify="flex-end">
              <Button
                type="button"
                variant="subtle"
                color="red"
                size="xs"
                aria-label={`Remove mapping ${index + 1}`}
                onClick={() => update(mappings.filter((_, i) => i !== index))}
              >
                Remove mapping
              </Button>
            </Group>
          </Stack>
        );
      })}
      <Group>
        <Button
          type="button"
          variant="light"
          disabled={isSilo && libraries.length === 0}
          onClick={() =>
            update([
              ...mappings,
              {
                local_path: "",
                remote_path: "",
                ...(isSilo ? { library_id: libraries[0].id } : {}),
              },
            ])
          }
        >
          Add mapping
        </Button>
      </Group>
    </Stack>
  );
}
