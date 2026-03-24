import { FunctionComponent, useCallback, useMemo } from "react";
import { faSync } from "@fortawesome/free-solid-svg-icons";
import { Checkbox } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { ColumnDef } from "@tanstack/react-table";
import { useSeries, useSeriesModification } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { BatchSyncItem } from "@/apis/raw/subtitles";
import { Toolbox } from "@/components";
import { QueryOverlay } from "@/components/async";
import { AudioList } from "@/components/bazarr";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import { useModals } from "@/modules/modals";
import MassEditor from "@/pages/views/MassEditor";

const SeriesMassEditor: FunctionComponent = () => {
  const query = useSeries();
  const mutation = useSeriesModification();
  const modals = useModals();

  const columns = useMemo<ColumnDef<Item.Series>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => {
          return (
            <Checkbox
              id="table-header-selection"
              indeterminate={table.getIsSomeRowsSelected()}
              checked={table.getIsAllRowsSelected()}
              onChange={table.getToggleAllRowsSelectedHandler()}
            ></Checkbox>
          );
        },
        cell: ({ row: { index, getIsSelected, getToggleSelectedHandler } }) => {
          return (
            <Checkbox
              id={`table-cell-${index}`}
              checked={getIsSelected()}
              onChange={getToggleSelectedHandler()}
              onClick={getToggleSelectedHandler()}
            ></Checkbox>
          );
        },
      },
      {
        header: "Name",
        accessorKey: "title",
      },
      {
        header: "Audio",
        accessorKey: "audio_language",
        cell: ({
          row: {
            original: { audio_language: audioLanguage },
          },
        }) => {
          return <AudioList audios={audioLanguage}></AudioList>;
        },
      },
      {
        header: "Languages Profile",
        accessorKey: "profileId",
        cell: ({
          row: {
            original: { profileId },
          },
        }) => {
          return <LanguageProfileName index={profileId}></LanguageProfileName>;
        },
      },
    ],
    [],
  );

  useDocumentTitle(`Series - ${useInstanceName()} (Mass Editor)`);

  const toolbarExtras = useCallback(
    (selections: Item.Series[]) => (
      <Toolbox.Button
        icon={faSync}
        disabled={selections.length === 0}
        onClick={() => {
          const items: BatchSyncItem[] = selections.map((s) => ({
            type: "series" as const,
            sonarrSeriesId: s.sonarrSeriesId,
          }));
          modals.openContextModal(MassSyncModal, { items });
        }}
      >
        Sync Subtitles
      </Toolbox.Button>
    ),
    [modals],
  );

  return (
    <QueryOverlay result={query}>
      <MassEditor
        columns={columns}
        data={query.data ?? []}
        mutation={mutation}
        toolbarExtras={toolbarExtras}
      ></MassEditor>
    </QueryOverlay>
  );
};

export default SeriesMassEditor;
