import { FunctionComponent } from "react";
import { Alert, MultiSelect, Stack } from "@mantine/core";
import { useJellyfinLibrariesQuery } from "@/apis/hooks/jellyfin";
import { BaseInput, useBaseInput } from "@/pages/Settings/utilities/hooks";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import styles from "./LibrarySelector.module.scss";

export type LibrarySelectorProps = BaseInput<string[]> & {
  label: string;
  libraryType: "movies" | "tvshows";
  settingKeyIds?: string;
  description?: string;
};

const LibrarySelector: FunctionComponent<LibrarySelectorProps> = (props) => {
  const { libraryType, description, label, settingKeyIds, ...baseProps } =
    props;
  const { value, update, rest } = useBaseInput(baseProps);

  const idsInput = useBaseInput({
    settingKey: settingKeyIds || "",
  });

  const jellyfinUrl = useSettingValue<string>("settings-jellyfin-url");
  const jellyfinApikey = useSettingValue<string>("settings-jellyfin-apikey");
  const verifySsl = useSettingValue<boolean>("settings-jellyfin-verify_ssl");
  const isConfigured = Boolean(jellyfinUrl && jellyfinApikey);

  const {
    data: librariesData,
    isLoading,
    error,
  } = useJellyfinLibrariesQuery(
    isConfigured,
    jellyfinUrl ?? undefined,
    jellyfinApikey ?? undefined,
    verifySsl ?? undefined,
  );

  const libraries = librariesData ?? [];
  const filtered = libraries.filter((lib) => lib.type === libraryType);
  const normalizedValue = Array.isArray(value) ? value : value ? [value] : [];

  const availableLibraries = filtered.map((lib) => lib.name);
  const staleLibraries = normalizedValue.filter(
    (name) => !availableLibraries.includes(name),
  );

  const selectData = [
    ...filtered.map((library) => ({
      value: library.name,
      label: library.name,
    })),
    ...staleLibraries.map((name) => ({
      value: name,
      label: `${name} (unavailable)`,
    })),
  ];

  const handleChange = (selectedNames: string[]) => {
    update(selectedNames);

    if (settingKeyIds) {
      const selectedIds = filtered
        .filter((lib) => selectedNames.includes(lib.name))
        .map((lib) => lib.id);
      idsInput.update(selectedIds);
    }
  };

  if (!isConfigured) {
    return (
      <Alert color="gray" variant="light" className={styles.alertMessage}>
        Enter your Jellyfin Server URL and API Key in the Connection section
        above; libraries will appear here once Bazarr+ can reach the server.
      </Alert>
    );
  }

  const libraryNoun = libraryType === "movies" ? "movie" : "TV show";

  return (
    <div className={styles.librarySelector}>
      <Stack gap="xs">
        <MultiSelect
          {...rest}
          label={label}
          description={description}
          data={selectData}
          value={normalizedValue}
          onChange={handleChange}
          searchable
          clearable
          className={styles.selectField}
        />
        {isLoading && (
          <Alert color="brand" variant="light" className={styles.alertMessage}>
            Fetching libraries from Jellyfin...
          </Alert>
        )}
        {error && !isLoading && (
          <Alert color="red" variant="light" className={styles.alertMessage}>
            Failed to load libraries from Jellyfin. Saved selections are shown
            above. Verify the Server URL and API Key in the Connection section,
            and that the API key's user has access to the libraries.
          </Alert>
        )}
        {!error && !isLoading && selectData.length === 0 && (
          <Alert color="gray" variant="light" className={styles.alertMessage}>
            No {libraryNoun} libraries found on this Jellyfin server. Make sure
            at least one is visible to the API key's user account.
          </Alert>
        )}
      </Stack>
    </div>
  );
};

export default LibrarySelector;
