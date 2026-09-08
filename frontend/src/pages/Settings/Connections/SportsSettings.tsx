/* eslint-disable camelcase */
import {
  Divider,
  NumberInput,
  Select,
  Stack,
  Switch,
  TagsInput,
  Text,
} from "@mantine/core";
import type { ArrSportsSettings } from "@/apis/raw/arrInstances";
import {
  seriesSyncOptions,
  upgradeOptions,
} from "@/pages/Settings/Scheduler/options";

export const SPORTS_SETTINGS_DEFAULTS: Required<ArrSportsSettings> = {
  sync_interval: 60,
  minimum_score: 70,
  wanted_search_frequency: 6,
  full_scan: "Daily",
  full_scan_day: 6,
  full_scan_hour: 4,
  only_monitored: false,
  sync_only_monitored_leagues: false,
  sync_only_monitored_events: false,
  excluded_tags: [],
  excluded_sports: [],
  search_on_sync: true,
  use_ffprobe_cache: true,
};

export default function SportsSettings({
  value,
  onChange,
}: {
  value: ArrSportsSettings;
  onChange: (value: ArrSportsSettings) => void;
}) {
  const settings = { ...SPORTS_SETTINGS_DEFAULTS, ...value };
  const set = <K extends keyof ArrSportsSettings>(
    key: K,
    next: ArrSportsSettings[K],
  ) => onChange({ ...value, [key]: next });
  return (
    <Stack gap="sm">
      <Divider label="Sports library" labelPosition="left" />
      <Select
        label="Library sync schedule"
        allowDeselect={false}
        data={[
          ...seriesSyncOptions.map((option) => ({
            label: option.label,
            value: String(option.value),
          })),
          ...(!seriesSyncOptions.some(
            (option) => option.value === settings.sync_interval,
          )
            ? [
                {
                  label: `${settings.sync_interval} Minutes`,
                  value: String(settings.sync_interval),
                },
              ]
            : []),
        ]}
        value={String(settings.sync_interval)}
        onChange={(next) => set("sync_interval", Number(next))}
      />
      {settings.sync_interval !== 52560000 && (
        <NumberInput
          label="Sync interval (minutes)"
          min={1}
          allowDecimal={false}
          value={settings.sync_interval}
          onChange={(next) => set("sync_interval", Number(next))}
        />
      )}
      <NumberInput
        label="Minimum sports subtitle score (%)"
        min={1}
        max={100}
        allowDecimal={false}
        value={settings.minimum_score}
        onChange={(next) => set("minimum_score", Number(next))}
      />
      <Select
        label="Search for missing sports subtitles"
        allowDeselect={false}
        data={upgradeOptions.map((option) => ({
          label: option.label,
          value: String(option.value),
        }))}
        value={String(settings.wanted_search_frequency)}
        onChange={(next) => set("wanted_search_frequency", Number(next))}
      />
      <Text size="xs" c="dimmed">
        Sports uses the movie score scale. Upgrade preferences are in Subtitles
        settings; the upgrade cadence is in Scheduler settings. Search after
        sync is a separate trigger.
      </Text>
      <Select
        label="Full subtitle scan"
        data={["Manually", "Daily", "Weekly"]}
        allowDeselect={false}
        value={settings.full_scan}
        onChange={(next) =>
          set("full_scan", next as ArrSportsSettings["full_scan"])
        }
      />
      {settings.full_scan === "Weekly" && (
        <Select
          label="Full scan day"
          data={[
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
          ].map((label, i) => ({ label, value: String(i) }))}
          value={String(settings.full_scan_day)}
          allowDeselect={false}
          onChange={(next) => set("full_scan_day", Number(next))}
        />
      )}
      {settings.full_scan !== "Manually" && (
        <NumberInput
          label="Full scan hour"
          min={0}
          max={23}
          allowDecimal={false}
          value={settings.full_scan_hour}
          onChange={(next) => set("full_scan_hour", Number(next))}
        />
      )}
      <Switch
        label="Search only monitored events"
        checked={settings.only_monitored}
        onChange={(event) => set("only_monitored", event.currentTarget.checked)}
      />
      <Switch
        label="Sync only monitored leagues"
        checked={settings.sync_only_monitored_leagues}
        onChange={(event) =>
          set("sync_only_monitored_leagues", event.currentTarget.checked)
        }
      />
      <Switch
        label="Sync only monitored events"
        checked={settings.sync_only_monitored_events}
        onChange={(event) =>
          set("sync_only_monitored_events", event.currentTarget.checked)
        }
      />
      <TagsInput
        label="Excluded tags"
        value={settings.excluded_tags}
        onChange={(next) => set("excluded_tags", next)}
      />
      <TagsInput
        label="Excluded sports"
        value={settings.excluded_sports}
        onChange={(next) => set("excluded_sports", next)}
      />
      <Switch
        label="Search after sync"
        checked={settings.search_on_sync}
        onChange={(event) => set("search_on_sync", event.currentTarget.checked)}
      />
      <Switch
        label="Cache media analysis"
        checked={settings.use_ffprobe_cache}
        onChange={(event) =>
          set("use_ffprobe_cache", event.currentTarget.checked)
        }
      />
    </Stack>
  );
}
