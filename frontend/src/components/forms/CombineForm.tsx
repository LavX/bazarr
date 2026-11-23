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
import { CombineScope, useCombineSubtitles } from "@/apis/hooks/combine";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/notification";
import { basename } from "@/utilities/files";

interface Props {
  scope: CombineScope;
  availableLanguages: string[];
  sourceInputs?: NonNullable<Api.CombineRequest["inputs"]>;
  onSubmit?: () => void;
}

const CombineForm: FunctionComponent<Props> = ({
  scope,
  availableLanguages,
  sourceInputs,
  onSubmit,
}) => {
  const [selected, setSelected] = useState<string[]>([]);
  const [format, setFormat] = useState<"srt" | "ass">("srt");
  const { mutateAsync, isPending } = useCombineSubtitles();
  const modals = useModals();

  const options = sourceInputs
    ? sourceInputs.map((input, index) => {
        const code = input.language.split(":")[0];
        const multipleFiles =
          sourceInputs.filter((row) => row.language.split(":")[0] === code)
            .length > 1;
        return {
          value: String(index),
          code,
          label: multipleFiles
            ? `${code.toUpperCase()} (${basename(input.path)})`
            : code.toUpperCase(),
        };
      })
    : availableLanguages.map((code) => ({
        value: code,
        code,
        label: code.toUpperCase(),
      }));
  const languageFor = (value: string) =>
    sourceInputs ? sourceInputs[Number(value)].language.split(":")[0] : value;
  const languages = selected.map(languageFor);
  const choicesFor = (index?: number) =>
    options.filter(
      (option) =>
        !languages.some((code, i) => i !== index && code === option.code),
    );

  const addLang = (value: string | null) => {
    if (
      value &&
      selected.length < 3 &&
      !languages.includes(languageFor(value))
    ) {
      setSelected([...selected, value]);
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
        body: {
          languages,
          format,
          ...(sourceInputs && {
            inputs: selected.map((value) => sourceInputs[Number(value)]),
          }),
        },
      });
      if (result.status === "built" && result.error) {
        // Published, but a follow-up step did not complete. Reporting this as
        // a plain success hid work the operator still has to do: the subtitle
        // can be missing from the event index until it is reindexed.
        showNotification(
          notification.warn(
            "Combined subtitle needs attention",
            `Saved to ${result.path}, but a follow-up step failed: ${result.error}`,
          ),
        );
      } else if (result.status === "built") {
        showNotification(
          notification.info(
            "Combined subtitle generated",
            `Saved to ${result.path}`,
          ),
        );
      } else if (result.status === "skipped") {
        showNotification(notification.warn("Skipped", result.reason ?? ""));
      } else if (result.status === "queued") {
        // A series combine runs as a queued job, which reports its progress
        // and outcome through Jobs.
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
              data={choicesFor(idx)}
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
            data={choicesFor()}
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
