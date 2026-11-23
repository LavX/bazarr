import { useCallback, useMemo, useRef, useState } from "react";
import { Group } from "@mantine/core";
import { faCheck, faLanguage, faUndo } from "@fortawesome/free-solid-svg-icons";
import { useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import { getLibraryRowKey } from "@/apis/raw/library";
import { Toolbox } from "@/components";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import { useModals } from "@/modules/modals";
import { sourceDetailApi, sourceDetailKeys } from "@/pages/SourceDetail/api";

interface ProfileRow {
  id: number;
  profileId: number | null;
  origin?: "arr" | "source";
}

export function useLibraryProfileEdit<T extends ProfileRow>(
  kind: "series" | "movies",
  selections: T[],
  mutateArr: (form: FormType.ModifyItem) => Promise<unknown>,
) {
  const client = useQueryClient();
  const modals = useModals();
  const [dirties, setDirties] = useState<T[]>([]);
  const [isSaving, setIsSaving] = useState(false);
  const savingRef = useRef(false);

  const setProfiles = useCallback(
    (profileId: number | null) => {
      setDirties((current) => {
        const edits = new Map(
          current.map((row) => [getLibraryRowKey(row), row]),
        );
        selections.forEach((row) => {
          const edited = { ...row, profileId };
          edits.set(getLibraryRowKey(edited), edited);
        });
        return [...edits.values()];
      });
    },
    [selections],
  );

  const cancel = useCallback(() => setDirties([]), []);

  const acknowledge = useCallback((saved: T[]) => {
    // Keep a newer edit to the same row if it changed while this save ran.
    setDirties((current) => current.filter((row) => !saved.includes(row)));
  }, []);

  const save = useCallback(async () => {
    if (savingRef.current || dirties.length === 0) return;
    savingRef.current = true;
    setIsSaving(true);
    const queryKey = kind === "series" ? QueryKeys.Series : QueryKeys.Movies;
    let arrChanged = false;
    let sourceChanged = false;

    try {
      const arrRows = dirties.filter((row) => row.origin !== "source");
      for (let offset = 0; offset < arrRows.length; offset += 1000) {
        const batch = arrRows.slice(offset, offset + 1000);
        await mutateArr({
          id: batch.map((row) => row.id),
          profileid: batch.map((row) => row.profileId),
        });
        acknowledge(batch);
        arrChanged = true;
      }

      const sourceRows = dirties.filter((row) => row.origin === "source");
      for (const row of sourceRows) {
        await sourceDetailApi.updateProfile(
          kind === "series" ? "groups" : "items",
          row.id,
          row.profileId,
        );
        acknowledge([row]);
        sourceChanged = true;
      }
    } finally {
      try {
        // Refresh once for the batch, including writes before a partial failure.
        // The existing Arr mutation already refreshes its own list and details.
        await Promise.all([
          ...(arrChanged || sourceChanged
            ? [
                client.invalidateQueries({
                  queryKey: [QueryKeys.System, QueryKeys.Badges],
                }),
              ]
            : []),
          ...(sourceChanged
            ? [
                client.invalidateQueries({ queryKey: [queryKey, "library"] }),
                client.invalidateQueries({ queryKey: sourceDetailKeys.root }),
              ]
            : []),
        ]);
      } finally {
        savingRef.current = false;
        setIsSaving(false);
      }
    }
  }, [acknowledge, client, dirties, kind, mutateArr]);

  const profileToolbar = useMemo(() => {
    if (selections.length === 0 && dirties.length === 0) return undefined;
    return (
      <Group gap="xs">
        <Toolbox.Button
          icon={faLanguage}
          disabled={selections.length === 0 || isSaving}
          onClick={() => {
            modals.openContextModal(ChangeProfileModal, {
              onSelect: setProfiles,
            });
          }}
        >
          Change Profile
        </Toolbox.Button>
        {dirties.length > 0 && (
          <>
            <Toolbox.Button icon={faUndo} onClick={cancel} disabled={isSaving}>
              Cancel
            </Toolbox.Button>
            <Toolbox.MutateButton
              icon={faCheck}
              promise={save}
              disabled={isSaving}
            >
              Save
            </Toolbox.MutateButton>
          </>
        )}
      </Group>
    );
  }, [
    cancel,
    dirties.length,
    isSaving,
    modals,
    save,
    selections.length,
    setProfiles,
  ]);

  return { dirties, setProfiles, cancel, save, isSaving, profileToolbar };
}
