import { Badge } from "@mantine/core";
import { faFolder, faServer } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import InstanceBadge from "./InstanceBadge";

interface Props {
  row: Wanted.RowOwner;
  labels: Map<MediaSourceKey, string>;
  arrNames: Map<number, string>;
  defaultArr: number | null;
}

export default function SourceBadge({
  row,
  labels,
  arrNames,
  defaultArr,
}: Props) {
  if (row.origin !== "source") {
    return (
      <InstanceBadge
        instanceId={row.arr_instance_id}
        defaultId={defaultArr}
        nameById={arrNames}
      />
    );
  }

  const removed = row.source_key === "source:removed";
  const name = labels.get(row.source_key) ?? row.source_name ?? "Media source";
  return (
    <Badge
      size="sm"
      variant="light"
      color={removed ? "gray" : "blue"}
      leftSection={
        <FontAwesomeIcon
          icon={row.source_kind === "folder" ? faFolder : faServer}
          aria-hidden
        />
      }
    >
      {removed ? `${name} (removed)` : name}
    </Badge>
  );
}
