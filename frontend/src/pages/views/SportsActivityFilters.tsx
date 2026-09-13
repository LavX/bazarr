import { FunctionComponent, ReactNode } from "react";
import { useSearchParams } from "react-router";
import { Button, Group, Select, Text, TextInput } from "@mantine/core";
import { useSportsAvailability } from "@/apis/hooks/sports";

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
  actionLoading,
}) => {
  const { instances } = useSportsAvailability();
  const [params, setParams] = useSearchParams();
  const ownerParam = params.get("instance");
  const owner = ownerParam ? Number(ownerParam) : undefined;
  const eventId = params.get("event_id");
  // With exactly one Sportarr there is nothing to choose, so its actions run
  // against it without making the user pick it first.
  const selectedOwner =
    owner ?? (instances.length === 1 ? instances[0].id : undefined);

  return (
    <>
      <Group align="end">
        <Select
          label="Instance"
          placeholder="All instances"
          clearable
          value={owner ? String(owner) : null}
          data={instances.map((instance) => ({
            value: String(instance.id),
            label: instance.name,
          }))}
          onChange={(value) => {
            const next = new URLSearchParams(params);
            // The event filter belongs to one instance's row, so it cannot
            // survive a change of instance.
            next.delete("event_id");
            next.delete("league");
            if (value) next.set("instance", value);
            else next.delete("instance");
            setParams(next);
          }}
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
            disabled={!selectedOwner || actionDisabled}
            loading={actionLoading}
            onClick={() => selectedOwner && onAction?.(selectedOwner)}
          >
            {actionLabel}
          </Button>
        )}
      </Group>
      {actionLabel && !selectedOwner && (
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
