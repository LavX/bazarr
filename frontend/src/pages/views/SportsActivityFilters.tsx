import { FunctionComponent, ReactNode } from "react";
import { useSearchParams } from "react-router";
import { Button, Group, Text, TextInput } from "@mantine/core";
import {
  useSportsActivityScope,
  useSportsAvailability,
} from "@/apis/hooks/sports";
import SourceFilter from "./SourceFilter";

interface Props {
  language: string;
  onLanguageChange: (value: string) => void;
  provider: string;
  onProviderChange: (value: string) => void;
  /** Extra filter control for one kind of page, such as History's Action. */
  extra?: ReactNode;
  /** The page's own action, run against the selected instance. */
  actionLabel?: string;
  actionColor?: string;
  actionDisabled?: boolean;
  onAction?: (owner: number) => void;
  onSourceAction?: (source: MediaSourceKey) => void;
  onAllSourcesAction?: () => void;
  actionLoading?: boolean;
}

// The filter bar shared by the sports History and Blacklist pages.
//
// The instance select writes to the URL rather than to local state: the event
// detail page links here with ?instance=&event_id=, and a filter bar holding
// its own copy would show one instance while the query used another.
const SportsActivityFilters: FunctionComponent<Props> = ({
  language,
  onLanguageChange,
  provider,
  onProviderChange,
  extra,
  actionLabel,
  actionColor,
  actionDisabled,
  onAction,
  onSourceAction,
  onAllSourcesAction,
  actionLoading,
}) => {
  const { instances, sourceActivity } = useSportsAvailability();
  const scope = useSportsActivityScope(true);
  const [params, setParams] = useSearchParams();
  const owner = scope.owner;
  const eventId = params.get("event_id");
  // With exactly one Sportarr there is nothing to choose, so its actions run
  // against it without making the user pick it first.
  const source = scope.selected?.startsWith("source:") ? scope.selected : null;
  const allSources =
    sourceActivity &&
    owner === undefined &&
    scope.selected === null &&
    onAllSourcesAction;
  const selectedOwner = source
    ? undefined
    : owner === undefined
      ? !sourceActivity && instances.length === 1
        ? instances[0].id
        : undefined
      : instances.find((instance) => instance.id === owner)?.id;

  return (
    <>
      <Group align="end">
        <SourceFilter
          labels={scope.labels}
          value={scope.selected}
          onChange={scope.onChange}
          includeSources={sourceActivity}
          showSingleSource={sourceActivity}
          showSingleOwner={!sourceActivity && instances.length > 0}
          showLabel={!sourceActivity}
        />
        <TextInput
          label="Language"
          placeholder="en or en:hi"
          value={language}
          onChange={(event) => onLanguageChange(event.currentTarget.value)}
        />
        <TextInput
          label="Provider"
          value={provider}
          onChange={(event) => onProviderChange(event.currentTarget.value)}
        />
        {extra}
        {actionLabel && (
          <Button
            color={actionColor}
            variant={actionColor ? "light" : undefined}
            disabled={
              !(selectedOwner || allSources || (source && onSourceAction)) ||
              actionDisabled
            }
            loading={actionLoading}
            onClick={() => {
              if (allSources) allSources();
              else if (source) onSourceAction?.(source);
              else if (selectedOwner) onAction?.(selectedOwner);
            }}
          >
            {actionLabel}
          </Button>
        )}
      </Group>
      {actionLabel &&
        !selectedOwner &&
        !allSources &&
        !(source && onSourceAction) && (
          <Text size="sm" c="dimmed">
            Select an instance to run an action for its library.
          </Text>
        )}
      {eventId && (
        <Group>
          <Text size="sm">Showing this event&apos;s records.</Text>
          <Button
            variant="subtle"
            size="xs"
            onClick={() => {
              const next = new URLSearchParams(params);
              next.delete("event_id");
              setParams(next);
            }}
          >
            Show all events
          </Button>
        </Group>
      )}
    </>
  );
};

export default SportsActivityFilters;
