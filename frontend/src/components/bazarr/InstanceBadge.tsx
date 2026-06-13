import { FunctionComponent } from "react";
import { Badge, Text, VisuallyHidden } from "@mantine/core";
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
// semantics are also exposed to assistive tech. We use a visually-hidden text
// node (not aria-label, which is unreliable on the roleless <p>/<div> these
// Mantine components render) so screen readers reliably announce the meaning.
const InstanceBadge: FunctionComponent<Props> = ({
  instanceId,
  defaultId,
  nameById,
}) => {
  if (instanceId == null) {
    return (
      <Text size="sm" c="dimmed">
        <span aria-hidden>–</span>
        <VisuallyHidden>No owning instance</VisuallyHidden>
      </Text>
    );
  }

  const name = nameById.get(instanceId) ?? `#${instanceId}`;
  const isDefault = instanceId === defaultId;
  const prefix = isDefault ? "Default instance: " : "Instance: ";

  return (
    <Badge
      size="sm"
      variant="light"
      color={isDefault ? "gray" : "grape"}
      leftSection={<FontAwesomeIcon icon={faServer} aria-hidden />}
    >
      <VisuallyHidden>{prefix}</VisuallyHidden>
      {name}
    </Badge>
  );
};

export default InstanceBadge;
