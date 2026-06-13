import { FunctionComponent } from "react";
import { Badge, Text } from "@mantine/core";
import { faServer } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

interface Props {
  // The owning arr instance id of the row, or null/undefined when unassigned.
  instanceId: number | null | undefined;
  // The default instance id for this kind, so its rows can be visually muted.
  defaultId: number | null;
  // Lookup of instance id to display name.
  nameById: Map<number, string>;
}

// Shows the owning Sonarr/Radarr instance for a table row (#156). Used by the
// Movies, Series and Wanted lists when more than one instance of a kind is
// configured. The default instance uses a muted grey badge and the others an
// accent badge so they stand out; because that distinction is colour-only, the
// semantics are also exposed to assistive tech via an aria-label. Rows with no
// owning instance render a dimmed dash with a "No owning instance" label rather
// than a bare en-dash, which a screen reader would otherwise read as "dash".
const InstanceBadge: FunctionComponent<Props> = ({
  instanceId,
  defaultId,
  nameById,
}) => {
  if (instanceId == null) {
    return (
      <Text size="sm" c="dimmed" aria-label="No owning instance">
        <span aria-hidden>–</span>
      </Text>
    );
  }

  const name = nameById.get(instanceId) ?? `#${instanceId}`;
  const isDefault = instanceId === defaultId;
  const label = isDefault ? `Default instance: ${name}` : `Instance: ${name}`;

  return (
    <Badge
      size="sm"
      variant="light"
      color={isDefault ? "gray" : "grape"}
      aria-label={label}
      leftSection={<FontAwesomeIcon icon={faServer} />}
    >
      {name}
    </Badge>
  );
};

export default InstanceBadge;
