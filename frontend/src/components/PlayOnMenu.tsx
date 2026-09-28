import { FunctionComponent, useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Button,
  Loader,
  Menu,
  Text,
  Tooltip,
  VisuallyHidden,
} from "@mantine/core";
import {
  faArrowUpRightFromSquare,
  faCirclePlay,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { UseQueryResult } from "@tanstack/react-query";
import {
  useMediaServerLinkAnswers,
  useMediaServerLinkCandidates,
  useMediaServerLinkDestinations,
} from "@/apis/hooks/mediaServerLinks";
import type {
  LinkMediaType,
  MediaServerLinkAnswer,
  MediaServerLinkTarget,
} from "@/apis/raw/mediaServerLinks";
import Action from "@/components/inputs/Action";

// A pointer resting this long reads as intent; sweeping across rows does not.
const HOVER_INTENT_MS = 200;
export const NO_CANDIDATE = "No media server covers this file's path";

type Props = {
  mediaType: LinkMediaType;
  id: number;
  /** What the control opens, the way a person reads it: a title, or S02E05. */
  itemLabel: string;
  variant: "toolbox" | "action";
  className?: string;
};

type Answer = UseQueryResult<MediaServerLinkAnswer>;

// Asked again after no answer, a row is checking rather than still failing.
// A found or missing item stays as it was while it is looked up again.
function isChecking(answer: Answer | undefined) {
  return (
    !answer ||
    answer.isPending ||
    (answer.isFetching &&
      answer.data?.state !== "resolved" &&
      answer.data?.state !== "not_found")
  );
}

function answerText(name: string, answer: Answer | undefined) {
  if (isChecking(answer)) return `Checking ${name}`;
  if (answer?.data?.state === "resolved") return `Play on ${name}`;
  if (answer?.data?.state === "not_found") return `Not found on ${name}`;
  return `${name} is unreachable`;
}

function useWarm() {
  const [warm, setWarm] = useState(false);
  // A closed menu hands focus back to its trigger, which warms it; that is
  // no reason to look its rows up again. Asking is.
  const [closed, setClosed] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return {
    warm,
    // While this is true the control waits for its rows' answers.
    looking: warm && !closed,
    start: () => setWarm(true),
    // Asking answers a resting pointer too, so its pending intent is dropped:
    // it would look the rows up again after a menu closed quickly.
    look: () => {
      window.clearTimeout(timer.current);
      setWarm(true);
      setClosed(false);
    },
    close: () => setClosed(true),
    intend: (then: () => void) => {
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(then, HOVER_INTENT_MS);
    },
    // Nothing waits for an answer any more, so nothing new is asked.
    cool: () => {
      window.clearTimeout(timer.current);
      setWarm(false);
      setClosed(false);
    },
  };
}

const PlayOnRow: FunctionComponent<{
  target: MediaServerLinkTarget;
  answer: Answer | undefined;
}> = ({ target, answer }) => {
  const url = answer?.data?.state === "resolved" ? answer.data.url : null;
  // One anchor from the first render to the answer, so the row is swapped in
  // place: nothing moves under the pointer, focus stays where it was, and the
  // arrow keys always have somewhere to go.
  return (
    <Menu.Item
      component="a"
      href={url ?? undefined}
      target="_blank"
      rel="noopener noreferrer"
      aria-disabled={url ? undefined : true}
      closeMenuOnClick={Boolean(url)}
      onClick={(event: React.MouseEvent) => {
        if (!url) event.preventDefault();
      }}
      c={url ? undefined : "dimmed"}
      leftSection={
        url ? (
          <FontAwesomeIcon icon={faArrowUpRightFromSquare} />
        ) : isChecking(answer) ? (
          <Loader size={12} />
        ) : null
      }
    >
      {answerText(target.display_name, answer)}
    </Menu.Item>
  );
};

const PlayOnMenu: FunctionComponent<Props> = ({
  mediaType,
  id,
  itemLabel,
  variant,
  className,
}) => {
  const destinations = useMediaServerLinkDestinations();
  const { warm, looking, start, look, close, intend, cool } = useWarm();
  const opened = useRef(false);
  const candidates = useMediaServerLinkCandidates(mediaType, id, warm);
  const targets = candidates.data ?? [];
  // A control that stops looking lets go of the lookups it still runs.
  const answers = useMediaServerLinkAnswers(targets, mediaType, id, looking);

  // Opening the menu, a resting pointer, or focus on the single link asks
  // again wherever the last answer is stale: at once for a server that did
  // not answer, after fifteen minutes for a found or missing item, as the
  // server's own cache does.
  const ask = () => {
    look();
    if (candidates.isError) void candidates.refetch();
    for (const answer of answers) {
      if (answer.isStale && !answer.isFetching) void answer.refetch();
    }
  };

  const configured = destinations.data ?? [];
  const menuShown = configured.length > 1;
  // A menu that goes away open, when the destinations drop to one or none,
  // never closes, so it is no longer counted as open.
  useEffect(() => {
    if (!menuShown) opened.current = false;
  }, [menuShown]);
  if (configured.length === 0) return null;

  // A pointer that moves on before anything opened was passing by, on its
  // way across a table as often as not. The lookups it began let go of their
  // connections, so a server that hangs cannot hold them.
  const leave = () => {
    if (!opened.current) cool();
  };
  const warmth = {
    onMouseEnter: () => intend(ask),
    onMouseLeave: leave,
    onFocus: ask,
  };

  if (configured.length === 1) {
    // One destination: the control is the link itself. Its element never
    // changes, so keyboard focus survives the answer arriving.
    const name = configured[0].display_name;
    const answer = answers[0];
    const url = answer?.data?.state === "resolved" ? answer.data.url : null;
    const status = !warm
      ? null
      : candidates.isPending
        ? `Checking ${name}`
        : candidates.isError
          ? `${name} is unreachable`
          : targets.length === 0
            ? NO_CANDIDATE
            : url
              ? null
              : answerText(name, answer);
    const link = {
      component: "a" as const,
      href: url ?? undefined,
      target: "_blank",
      rel: "noopener noreferrer",
      tabIndex: 0,
      // A name begins with the words a control shows, so speech input can
      // reach it by them. The icon-only row action shows none.
      "aria-label":
        variant === "toolbox"
          ? `Play on ${name}: ${itemLabel}`
          : `Play ${itemLabel} on ${name}`,
      "aria-disabled": url ? undefined : true,
      onClick: (event: React.MouseEvent) => {
        if (!url) {
          event.preventDefault();
          ask();
        }
      },
      ...warmth,
    };
    return (
      <>
        <Tooltip label={status ?? `Play on ${name}`} openDelay={300}>
          {variant === "toolbox" ? (
            <Button
              {...link}
              color="dark"
              variant="subtle"
              leftSection={<FontAwesomeIcon icon={faCirclePlay} />}
            >
              <Text size="xs">{`Play on ${name}`}</Text>
            </Button>
          ) : (
            <ActionIcon {...link} className={className}>
              <FontAwesomeIcon icon={faCirclePlay} />
            </ActionIcon>
          )}
        </Tooltip>
        {warm && <VisuallyHidden role="status">{status ?? ""}</VisuallyHidden>}
      </>
    );
  }

  const label =
    variant === "toolbox"
      ? `Play on a media server: ${itemLabel}`
      : `Play ${itemLabel} on a media server`;
  // Focus only warms the menu trigger: the menu hands focus back to it each
  // time it closes, which is no reason to ask again. Opening it is.
  const triggerWarmth = { ...warmth, onFocus: start };
  const pending = answers.filter((answer) => isChecking(answer)).length;
  return (
    // A closed menu gives back the connections its rows still hold; opening
    // it again asks those rows again.
    <Menu
      shadow="md"
      width={260}
      position="bottom-end"
      onOpen={() => {
        opened.current = true;
        ask();
      }}
      onClose={() => {
        opened.current = false;
        close();
      }}
    >
      <Menu.Target>
        {variant === "toolbox" ? (
          <Button
            color="dark"
            variant="subtle"
            aria-label={label}
            leftSection={<FontAwesomeIcon icon={faCirclePlay} />}
            {...triggerWarmth}
          >
            <Text size="xs">Play on</Text>
          </Button>
        ) : (
          <Action
            icon={faCirclePlay}
            label={label}
            className={className}
            {...triggerWarmth}
          />
        )}
      </Menu.Target>
      <Menu.Dropdown>
        {candidates.isPending ? (
          <Menu.Label role="status">Checking your media servers</Menu.Label>
        ) : candidates.isError ? (
          <Menu.Label role="status">
            Your media servers could not be checked
          </Menu.Label>
        ) : targets.length === 0 ? (
          <Menu.Label role="status">{NO_CANDIDATE}</Menu.Label>
        ) : (
          <>
            {targets.map((target, index) => (
              <PlayOnRow
                key={target.instance_id}
                target={target}
                answer={answers[index]}
              />
            ))}
            <VisuallyHidden role="status">
              {pending
                ? `Checking ${pending} of ${targets.length} media servers`
                : "Media servers checked"}
            </VisuallyHidden>
          </>
        )}
      </Menu.Dropdown>
    </Menu>
  );
};

export default PlayOnMenu;
