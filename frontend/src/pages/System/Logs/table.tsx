import { FunctionComponent, useMemo } from "react";
import { Box } from "@mantine/core";
import { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import {
  faBug,
  faCode,
  faExclamationCircle,
  faInfoCircle,
  faLayerGroup,
  faQuestion,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { Action } from "@/components";
import SimpleTable from "@/components/tables/SimpleTable";
import { useModals } from "@/modules/modals";
import SystemLogModal from "./modal";
import styles from "./Logs.module.scss";

interface Props {
  logs: System.Log[];
  emptyText: string;
}

function mapTypeToIcon(type: System.LogType): IconDefinition {
  switch (type) {
    case "DEBUG":
      return faCode;
    case "CRITICAL":
    case "ERROR":
      return faBug;
    case "INFO":
      return faInfoCircle;
    case "WARNING":
      return faExclamationCircle;
    default:
      return faQuestion;
  }
}

// The server sends one page, so the rows are shown as they arrive, newest
// first, and the page control beside this table asks for the next one.
const Table: FunctionComponent<Props> = ({ logs, emptyText }) => {
  const modals = useModals();

  const columns = useMemo<ColumnDef<System.Log>[]>(
    () => [
      {
        accessorKey: "type",
        cell: ({
          row: {
            original: { type },
          },
        }) => (
          <FontAwesomeIcon
            icon={mapTypeToIcon(type)}
            title={type}
          ></FontAwesomeIcon>
        ),
      },
      {
        header: "Message",
        accessorKey: "message",
        cell: ({
          row: {
            original: { message },
          },
        }) => <span className={styles.message}>{message}</span>,
      },
      {
        header: "Date",
        accessorKey: "timestamp",
        cell: ({
          row: {
            original: { timestamp },
          },
        }) => <span className={styles.date}>{timestamp}</span>,
      },
      {
        accessorKey: "exception",
        cell: ({
          row: {
            original: { exception },
          },
        }) => {
          if (exception) {
            return (
              <Action
                label="Detail"
                icon={faLayerGroup}
                onClick={() =>
                  modals.openContextModal(SystemLogModal, { stack: exception })
                }
              ></Action>
            );
          } else {
            return null;
          }
        },
      },
    ],
    [modals],
  );

  return (
    <Box className={styles.logs}>
      <SimpleTable
        columns={columns}
        data={logs}
        tableStyles={{ emptyText }}
      ></SimpleTable>
    </Box>
  );
};

export default Table;
