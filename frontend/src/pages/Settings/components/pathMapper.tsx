import { FunctionComponent, useCallback, useMemo } from "react";
import { Button } from "@mantine/core";
import { faArrowCircleRight, faTrash } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { capitalize } from "lodash";
import { Action, FileBrowser } from "@/components";
import SimpleTable from "@/components/tables/SimpleTable";
import {
  moviesEnabledKey,
  pathMappingsKey,
  pathMappingsMovieKey,
  pathMappingsSportsKey,
  seriesEnabledKey,
  sportsEnabledKey,
} from "@/pages/Settings/keys";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import { useArrayAction } from "@/utilities";
import { Message } from "./Message";

type SupportType = "sonarr" | "radarr" | "sports";

function getSupportKey(type: SupportType) {
  if (type === "sonarr") {
    return pathMappingsKey;
  } else if (type === "radarr") {
    return pathMappingsMovieKey;
  } else {
    return pathMappingsSportsKey;
  }
}

function getEnabledKey(type: SupportType) {
  if (type === "sonarr") {
    return seriesEnabledKey;
  } else if (type === "radarr") {
    return moviesEnabledKey;
  } else {
    return sportsEnabledKey;
  }
}

interface PathMappingItem {
  from: string;
  to: string;
}

interface TableProps {
  type: SupportType;
}

export const PathMappingTable: FunctionComponent<TableProps> = ({ type }) => {
  const key = getSupportKey(type);
  const items = useSettingValue<[string, string][]>(key);

  const enabledKey = getEnabledKey(type);
  const enabled = useSettingValue<boolean>(enabledKey, { original: true });

  const { setValue } = useFormActions();

  const updateRow = useCallback(
    (newItems: PathMappingItem[]) => {
      setValue(
        newItems.map((v) => [v.from, v.to]),
        key,
      );
    },
    [key, setValue],
  );

  const addRow = useCallback(() => {
    if (items) {
      const newItems = [...items, ["", ""]];
      setValue(newItems, key);
    }
  }, [items, key, setValue]);

  const data = useMemo<PathMappingItem[]>(
    () => items?.map((v) => ({ from: v[0], to: v[1] })) ?? [],
    [items],
  );

  const action = useArrayAction<PathMappingItem>((fn) => {
    updateRow(fn(data));
  });

  const columns = useMemo<ColumnDef<PathMappingItem>[]>(
    () => [
      {
        header: capitalize(type),
        accessorKey: "from",
        cell: ({ row: { original, index } }) => {
          // These are the GLOBAL (Phase-12-gated) path-mapping settings, so
          // the browse intentionally targets the DEFAULT server (instanceId
          // unset, #156). For a sports mapping that is the default Sportarr
          // instance, whose browser seeds its initial listing with the root
          // folders that have already been synced. When a per-instance
          // path-mapping UI lands, pass the selected instance id here so it
          // browses that instance's server.
          return (
            <FileBrowser
              type={type === "sports" ? "sportarr" : type}
              defaultValue={original.from}
              onChange={(path) => {
                action.mutate(index, { ...original, from: path });
              }}
            ></FileBrowser>
          );
        },
      },
      {
        id: "arrow",
        cell: () => (
          <FontAwesomeIcon icon={faArrowCircleRight}></FontAwesomeIcon>
        ),
      },
      {
        header: "Bazarr",
        accessorKey: "to",
        cell: ({ row: { original, index } }) => {
          return (
            <FileBrowser
              defaultValue={original.to}
              type="bazarr"
              onChange={(path) => {
                action.mutate(index, { ...original, to: path });
              }}
            ></FileBrowser>
          );
        },
      },
      {
        id: "action",
        accessorKey: "to",
        cell: ({ row: { index } }) => {
          return (
            <Action
              label="Remove"
              icon={faTrash}
              onClick={() => action.remove(index)}
            ></Action>
          );
        },
      },
    ],
    [action, type],
  );

  if (enabled) {
    return (
      <>
        <SimpleTable
          tableStyles={{ emptyText: "No mapping" }}
          columns={columns}
          data={data}
        ></SimpleTable>
        <Button fullWidth onClick={addRow}>
          Add
        </Button>
      </>
    );
  } else {
    return (
      <Message>
        Path Mappings will be available after staged changes are saved
      </Message>
    );
  }
};
