import { FunctionComponent, useMemo, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Divider,
  Group,
  Radio,
  Select,
  Stack,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { useModals, withModal } from "@/modules/modals";
import { useEnabledLanguages } from "@/utilities/languages";
import type { WantedItem } from "./MassTranslateForm";

interface Props {
  items: WantedItem[];
  onComplete?: () => void;
}

function itemArrInstanceId(item: WantedItem) {
  const snakeValue = (item as { arr_instance_id?: unknown }).arr_instance_id;
  const camelValue = (item as { arrInstanceId?: unknown }).arrInstanceId;
  const value = snakeValue ?? camelValue;
  return typeof value === "number" ? value : undefined;
}

const MassCombineForm: FunctionComponent<Props> = ({ items, onComplete }) => {
  const modals = useModals();
  const { mutateAsync } = useCombineSubtitles();
  const { data: enabledLangs } = useEnabledLanguages();

  const [selected, setSelected] = useState<string[]>([]);
  const [format, setFormat] = useState<"srt" | "ass">("srt");
  const [running, setRunning] = useState(false);

  const langOptions = useMemo(
    () =>
      (enabledLangs ?? []).map((l) => ({
        value: l.code2,
        label: `${l.name} (${l.code2.toUpperCase()})`,
      })),
    [enabledLangs],
  );

  const addLang = (code: string | null) => {
    if (code && selected.length < 3 && !selected.includes(code)) {
      setSelected([...selected, code]);
    }
  };

  const removeAt = (idx: number) => {
    const next = [...selected];
    next.splice(idx, 1);
    setSelected(next);
  };

  const canSubmit = selected.length >= 2 && items.length > 0 && !running;

  const submit = async () => {
    if (selected.length < 2) return;

    if (items.length >= 50) {
      const confirmed = window.confirm(
        `This will attempt to combine subtitles for ${items.length} items. Continue?`,
      );
      if (!confirmed) return;
    }

    setRunning(true);
    let built = 0;
    let skipped = 0;
    let failed = 0;

    for (const item of items) {
      let scope:
        | { kind: "movie"; radarrId: number; arrInstanceId?: number }
        | { kind: "episode"; episodeId: number; arrInstanceId?: number }
        | { kind: "series"; seriesId: number; arrInstanceId?: number };

      if (item.type === "movie") {
        scope = {
          kind: "movie",
          radarrId: item.radarrId,
          arrInstanceId: itemArrInstanceId(item),
        };
      } else if (item.type === "series") {
        scope = {
          kind: "series",
          seriesId: item.sonarrSeriesId,
          arrInstanceId: itemArrInstanceId(item),
        };
      } else {
        scope = {
          kind: "episode",
          episodeId: item.sonarrEpisodeId,
          arrInstanceId: itemArrInstanceId(item),
        };
      }

      try {
        const result = await mutateAsync({
          scope,
          body: { languages: selected, format },
        });

        if (result.status === "batch_complete") {
          built += result.built ?? 0;
          skipped += result.skipped ?? 0;
          failed += result.failed ?? 0;
        } else if (result.status === "built") {
          built += 1;
        } else if (result.status === "skipped") {
          skipped += 1;
        } else {
          failed += 1;
        }
      } catch {
        failed += 1;
      }
    }

    setRunning(false);

    notifications.show({
      title: "Combine complete",
      message: `Built ${built}, skipped ${skipped}, failed ${failed}`,
      color: failed > 0 ? "yellow" : "green",
    });

    onComplete?.();
    modals.closeSelf();
  };

  return (
    <Stack>
      <Alert>
        <Text size="sm">
          Combine 2 to 3 languages into one stacked subtitle file for{" "}
          <strong>{items.length}</strong> item(s). Items missing any of the
          chosen source languages are skipped silently. This action only
          composes files already on disk; it never triggers a translation.
        </Text>
      </Alert>

      {items.length > 0 && items.length <= 5 && (
        <Stack gap="xs">
          <Text size="sm" fw={500}>
            Selected items:
          </Text>
          <Group gap="xs">
            {items.map((item, idx) => (
              <Badge key={idx} variant="light" size="sm">
                {item.type === "episode"
                  ? `${item.seriesTitle} - ${item.episodeTitle}`
                  : item.title}
              </Badge>
            ))}
          </Group>
        </Stack>
      )}

      {items.length > 5 && (
        <Text size="sm" c="var(--bz-text-tertiary)">
          {items.length} items selected
        </Text>
      )}

      <Text size="sm" fw={500}>
        Languages (in display order, max 3, first is primary)
      </Text>
      <Stack gap="xs">
        {selected.map((code, idx) => (
          <Group key={`${code}-${idx}`}>
            <Select
              data={langOptions}
              value={code}
              onChange={(next) => {
                if (next) {
                  const updated = [...selected];
                  updated[idx] = next;
                  setSelected(updated);
                }
              }}
              style={{ width: 220 }}
            />
            <Button size="xs" variant="subtle" onClick={() => removeAt(idx)}>
              Remove
            </Button>
          </Group>
        ))}
        {selected.length < 3 && (
          <Select
            placeholder="Add language"
            data={langOptions.filter((o) => !selected.includes(o.value))}
            onChange={addLang}
            style={{ width: 220 }}
          />
        )}
      </Stack>

      <Radio.Group
        label="Output format"
        value={format}
        onChange={(f) => setFormat(f as "srt" | "ass")}
      >
        <Group mt="xs">
          <Radio value="srt" label="SRT stacked" />
          <Radio value="ass" label="ASS positioned" />
        </Group>
      </Radio.Group>

      <Divider />

      <Group justify="space-between">
        <Button variant="default" onClick={() => modals.closeSelf()}>
          Cancel
        </Button>
        <Button onClick={submit} loading={running} disabled={!canSubmit}>
          Combine {items.length} Item(s)
        </Button>
      </Group>
    </Stack>
  );
};

export const MassCombineModal = withModal(MassCombineForm, "mass-combine", {
  title: "Mass Combine Subtitles",
  size: "md",
});

export default MassCombineForm;
