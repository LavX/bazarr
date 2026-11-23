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
  initialRequest?: Api.CombineRequest;
  onSubmit?: () => void;
}

const CombineForm: FunctionComponent<Props> = ({
  scope,
  availableLanguages,
  sourceInputs,
  initialRequest,
  onSubmit,
}) => {
  const [selected, setSelected] = useState<
    { code: string; value: string | null }[]
  >(() =>
    (initialRequest?.languages ?? []).map((code) => {
      const input = initialRequest?.inputs?.find(
        (row) => row.language.split(":")[0] === code,
      );
      const index = sourceInputs?.findIndex(
        (row) => row.path === input?.path && row.language === input.language,
      );
      return {
        code,
        value: sourceInputs
          ? index !== undefined && index >= 0
            ? String(index)
            : null
          : code,
      };
    }),
  );
  const [format, setFormat] = useState<"srt" | "ass">(
    initialRequest?.format ?? "srt",
  );
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
  const languages = selected.map((selection) => selection.code);
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
      setSelected([...selected, { code: languageFor(value), value }]);
    }
  };

  const removeAt = (idx: number) => {
    const next = [...selected];
    next.splice(idx, 1);
    setSelected(next);
  };

  const canSubmit =
    selected.length >= 2 &&
    selected.every((selection) => selection.value !== null);

  const submit = async () => {
    try {
      const result = await mutateAsync({
        scope,
        body: {
          languages,
          format,
          ...(sourceInputs && {
            inputs: selected.map(
              (selection) => sourceInputs[Number(selection.value)],
            ),
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
        {selected.map((selection, idx) => (
          <Group key={idx}>
            <Select
              aria-label={`Input ${idx + 1}`}
              placeholder={`Choose ${selection.code.toUpperCase()} input`}
              data={choicesFor(idx)}
              value={selection.value}
              onChange={(next) => {
                if (next) {
                  const updated = [...selected];
                  updated[idx] = { code: languageFor(next), value: next };
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
