import { FunctionComponent } from "react";
import { Badge, Tooltip, VisuallyHidden } from "@mantine/core";
import { faTriangleExclamation } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

const EXPLANATION =
  "Subtitles for this item exist, but only for a different release type than " +
  "the one you have. Grabbing that release would likely give you a subtitle.";

// Marks a wanted item whose own release type has no acceptable subtitle while
// another release type does. The badge carries its own text rather than colour
// alone, and the explanation is exposed to assistive tech instead of living
// only in the hover tooltip.
const ReleaseMismatchBadge: FunctionComponent = () => {
  return (
    <Tooltip label={EXPLANATION} multiline w={260} withArrow>
      <Badge
        color="yellow"
        variant="light"
        leftSection={<FontAwesomeIcon icon={faTriangleExclamation} />}
      >
        <span>Release mismatch</span>
        <VisuallyHidden>{EXPLANATION}</VisuallyHidden>
      </Badge>
    </Tooltip>
  );
};

export default ReleaseMismatchBadge;
