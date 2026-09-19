import {
  FunctionComponent,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { Anchor, Badge, Button, Card, Group, Text, Title } from "@mantine/core";
import {
  faArrowUpRightFromSquare,
  faChevronDown,
  faChevronUp,
  faXmark,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSystemAnnouncementsAddDismiss } from "@/apis/hooks";
import { MutateAction } from "@/components/async";
import {
  ANNOUNCEMENT_LABELS,
  classifyAnnouncement,
  linkHost,
  splitAnnouncementText,
  splitLinks,
} from "./announcementText";
import classes from "./index.module.css";

/** Enough of a body to tell whether it is worth opening, no more. */
const BODY_LINES = 4;

interface Props {
  announcement: System.Announcements;
}

const AnnouncementCard: FunctionComponent<Props> = ({ announcement }) => {
  const { dismissible, hash, link, text, timestamp } = announcement;
  const addDismiss = useSystemAnnouncementsAddDismiss();
  const [expanded, setExpanded] = useState(false);
  const titleId = useId();
  const bodyId = useId();

  const { body, headline } = useMemo(() => splitAnnouncementText(text), [text]);
  const kind = useMemo(() => classifyAnnouncement(text), [text]);
  const segments = useMemo(() => splitLinks(body), [body]);
  const { overflowing, ref } = useOverflowingBody(body);
  const host = linkHost(link);

  return (
    <Card
      component="article"
      aria-labelledby={headline ? titleId : undefined}
      className={classes.card}
      p="lg"
    >
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm">
        <Badge className={classes.kind}>{ANNOUNCEMENT_LABELS[kind]}</Badge>
        <Group gap="xs" align="center" wrap="nowrap">
          <Text className={classes.age}>{timestamp}</Text>
          <MutateAction
            label="Dismiss announcement"
            disabled={!dismissible}
            icon={faXmark}
            mutation={addDismiss}
            args={() => ({
              hash: hash,
            })}
          ></MutateAction>
        </Group>
      </Group>

      {headline && (
        <Title order={3} size="h4" id={titleId} className={classes.headline}>
          {headline}
        </Title>
      )}

      {body && (
        <Text
          ref={ref}
          id={bodyId}
          lineClamp={expanded ? undefined : BODY_LINES}
          className={classes.body}
        >
          {segments.map((segment, index) =>
            segment.link ? (
              <Anchor
                key={index}
                href={segment.link}
                target="_blank"
                rel="noopener noreferrer"
                underline="always"
              >
                {segment.text}
              </Anchor>
            ) : (
              <span key={index}>{segment.text}</span>
            ),
          )}
        </Text>
      )}

      {(overflowing || link) && (
        <Group
          justify={overflowing && link ? "space-between" : "flex-start"}
          align="center"
          gap="sm"
          mt="sm"
        >
          {overflowing && (
            <Button
              variant="subtle"
              size="compact-sm"
              className={classes.toggle}
              aria-expanded={expanded}
              aria-controls={bodyId}
              onClick={() => setExpanded(!expanded)}
              rightSection={
                <FontAwesomeIcon
                  icon={expanded ? faChevronUp : faChevronDown}
                  size="xs"
                />
              }
            >
              {expanded ? "Show less" : "Show more"}
            </Button>
          )}
          {link && (
            <Anchor
              href={link}
              target="_blank"
              rel="noopener noreferrer"
              className={classes.link}
              title={link}
              aria-label={`Read more at ${host ?? link}`}
              underline="always"
            >
              {host ?? "Read more"}
              <FontAwesomeIcon icon={faArrowUpRightFromSquare} size="xs" />
            </Anchor>
          )}
        </Group>
      )}
    </Card>
  );
};

/**
 * Whether the clamped body is actually cut off, so a control is not offered
 * for text the reader can already see the end of. Measured rather than
 * estimated from a character count, because how much fits depends on the
 * viewport, and latched once true so the control that collapses the body
 * cannot vanish the moment it is used.
 */
function useOverflowingBody(text: string) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [overflowing, setOverflowing] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node) {
      return;
    }

    const measure = () => {
      if (node.scrollHeight > node.clientHeight + 1) {
        setOverflowing(true);
      }
    };

    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [text]);

  return { overflowing, ref };
}

export default AnnouncementCard;
