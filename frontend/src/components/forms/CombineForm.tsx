import { FunctionComponent, useState } from "react";
import {
  Alert,
  Button,
  Divider,
  Group,
  Radio,
  Select,
  Stack,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/task";

type Scope =
  | { kind: "movie"; radarrId: number }
  | { kind: "episode"; episodeId: number }
  | { kind: "series"; seriesId: number };

interface Props {
  scope: Scope;
  availableLanguages: string[];
  onSubmit?: () => void;
}

const CombineForm: FunctionComponent<Props> = ({
  scope,
  availableLanguages,
  onSubmit,
}) => {
  const [selected, setSelected] = useState<string[]>([]);
  const [format, setFormat] = useState<"srt" | "ass">("srt");
  const { mutateAsync, isPending } = useCombineSubtitles();
  const modals = useModals();

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

  const canSubmit = selected.length >= 2;

  const submit = async () => {
    try {
      const result = await mutateAsync({
        scope,
        body: { languages: selected, format },
      });
      if (result.status === "built") {
        showNotification(
          notification.info(
            "Combined subtitle generated",
            `Saved to ${result.path}`,
          ),
        );
      } else if (result.status === "skipped") {
        showNotification(notification.warn("Skipped", result.reason ?? ""));
      } else if (result.status === "batch_complete") {
        const built = result.built ?? 0;
        const skipped = result.skipped ?? 0;
        const failed = result.failed ?? 0;
        showNotification(
          notification.info(
            "Series combine complete",
            `Built ${built}, skipped ${skipped}, failed ${failed}`,
          ),
        );
      } else {
        showNotification(
          notification.error("Combine failed", result.error ?? ""),
        );
      }
      onSubmit?.();
      modals.closeSelf();
    } catch (e) {
      showNotification(notification.error("Request failed", String(e)));
    }
  };

  return (
    <Stack>
      <Alert>
        Pick 2 to 3 languages from those already downloaded for this video.
        Order is the display order; first language is primary.
      </Alert>
      <Stack gap="xs">
        {selected.map((code, idx) => (
          <Group key={`${code}-${idx}`}>
            <Select
              data={availableLanguages.map((c) => ({
                value: c,
                label: c.toUpperCase(),
              }))}
              value={code}
              onChange={(next) => {
                if (next) {
                  const updated = [...selected];
                  updated[idx] = next;
                  setSelected(updated);
                }
              }}
              style={{ width: 160 }}
            />
            <Button size="xs" variant="subtle" onClick={() => removeAt(idx)}>
              Remove
            </Button>
          </Group>
        ))}
        {selected.length < 3 && (
          <Select
            placeholder="Add language"
            data={availableLanguages
              .filter((c) => !selected.includes(c))
              .map((c) => ({ value: c, label: c.toUpperCase() }))}
            onChange={addLang}
            style={{ width: 160 }}
          />
        )}
      </Stack>
      <Radio.Group
        label="Output format"
        value={format}
        onChange={(f) => setFormat(f as "srt" | "ass")}
      >
        <Group>
          <Radio value="srt" label="SRT stacked" />
          <Radio value="ass" label="ASS positioned" />
        </Group>
      </Radio.Group>
      <Divider />
      <Button disabled={!canSubmit} loading={isPending} onClick={submit}>
        Generate
      </Button>
    </Stack>
  );
};

export const CombineModal = withModal(CombineForm, "combine-subtitles", {
  title: "Combine subtitles",
});

export default CombineForm;
