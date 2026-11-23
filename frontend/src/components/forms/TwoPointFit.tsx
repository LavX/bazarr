import { FunctionComponent, useEffect, useMemo } from "react";
import {
  Alert,
  Button,
  Card,
  Divider,
  Group,
  LoadingOverlay,
  NumberInput,
  Stack,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { showNotification } from "@mantine/notifications";
import {
  isShownConflict,
  isShownSubtitleActionError,
  useSubtitleAction,
  useSubtitleContent,
  useSubtitleContents,
} from "@/apis/hooks";
import { Selector } from "@/components/inputs";
import { useModals, withModal } from "@/modules/modals";
import { notification } from "@/modules/notification";
import { detectFormat, getParser } from "@/pages/SubtitleEditor/parsers";
import { useSelectorOptions } from "@/utilities";

function convertToAction(
  r: { hour: number; min: number; sec: number; ms: number }, // offset to zero
  o: { hour: number; min: number; sec: number; ms: number }, // offset
  s: { from: number; to: number }, // scale
) {
  return `two_point_fit(rh=${r.hour},rm=${r.min},rs=${r.sec},rms=${r.ms},oh=${o.hour},om=${o.min},os=${o.sec},oms=${o.ms},from=${s.from},to=${s.to})`;
}

const totalMs = (t: { hour: number; min: number; sec: number; ms: number }) =>
  t.hour * 3600000 + t.min * 60000 + t.sec * 1000 + t.ms;

const lineStartMs = (t: SubtitleContents.LineTime) =>
  t.total_seconds * 1000 + Math.round(t.microseconds / 1000);

const lineStartToTime = (t: SubtitleContents.LineTime) => ({
  hour: t.hours,
  min: t.minutes,
  sec: t.seconds,
  ms: Math.round(t.microseconds / 1000),
});

function millisecondsToLineTime(value: number): SubtitleContents.LineTime {
  const seconds = Math.floor(value / 1000);
  return {
    hours: Math.floor(seconds / 3600),
    minutes: Math.floor((seconds % 3600) / 60),
    seconds: seconds % 60,
    // eslint-disable-next-line camelcase -- Existing subtitle line API shape.
    total_seconds: seconds,
    microseconds: Math.round((value % 1000) * 1000),
  };
}

interface Props {
  selections: FormType.ModifySubtitle[];
  onSubmit?: VoidFunction;
}

const TwoPointFitForm: FunctionComponent<Props> = ({
  selections,
  onSubmit,
}) => {
  const { mutate, mutateAsync, isPending } = useSubtitleAction();
  const modals = useModals();

  const subtitle = selections[0];
  const isSource = subtitle.type === "source";
  const pathQuery = useSubtitleContents(subtitle.path, !isSource);
  const sourceQuery = useSubtitleContent(
    isSource ? "source" : undefined,
    subtitle.id,
    subtitle.sourceLanguage ?? subtitle.language,
    undefined,
    undefined,
    undefined,
    isSource
      ? () => subtitle.validateSource?.(true) ?? Promise.resolve()
      : undefined,
  );
  const query = isSource ? sourceQuery : pathQuery;
  const loading = query.isLoading || (isSource && sourceQuery.isFetching);
  const lines = useMemo(() => {
    if (!isSource) return pathQuery.data ?? [];
    const content = sourceQuery.data?.data;
    if (!content) return [];
    return getParser(detectFormat(`subtitle.${content.format}`))
      .parse(content.content)
      .cues.map(
        (cue, index): SubtitleContents.Line => ({
          index: index + 1,
          content: cue.text,
          proprietary: "",
          start: millisecondsToLineTime(cue.startMs),
          end: millisecondsToLineTime(cue.endMs),
        }),
      );
  }, [isSource, pathQuery.data, sourceQuery.data]);

  const form = useForm({
    initialValues: {
      first: {
        line: null as SubtitleContents.Line | null,
        to: { hour: 0, min: 0, sec: 0, ms: 0 },
      },
      last: {
        line: null as SubtitleContents.Line | null,
        to: { hour: 0, min: 0, sec: 0, ms: 0 },
      },
    },
    validate: {
      first: {
        line: (v) => (v == null ? "Please select a line" : null),
      },
      last: {
        line: (v, values) => {
          if (v == null) return "Please select a line";
          if (values.first.line != null && v.index === values.first.line.index)
            return "Must be a different line than the first";
          return null;
        },
      },
    },
  });

  // Preselect default lines when data loads
  useEffect(() => {
    if (lines.length === 0) return;

    const firstLine = lines.length < 50 ? lines[0] : lines[9];
    const lastLine =
      lines.length < 50 ? lines[lines.length - 1] : lines[lines.length - 10];

    form.setFieldValue("first.line", firstLine);
    form.setFieldValue("first.to", lineStartToTime(firstLine.start));
    form.setFieldValue("last.line", lastLine);
    form.setFieldValue("last.to", lineStartToTime(lastLine.start));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lines]);

  const wrongOrder = useMemo(() => {
    const { first, last } = form.values;
    if (first.line == null || last.line == null) return false;
    return lineStartMs(first.line.start) >= lineStartMs(last.line.start);
  }, [form.values]);

  const decimals = lines.length.toString().length;
  const options = useSelectorOptions(
    lines,
    (v) =>
      `${String(v.index).padStart(decimals, "0")}: ${String(
        v.content,
      ).replaceAll("\n", " ")}`,
    (v) => String(v.index),
  );

  return (
    <form
      onSubmit={form.onSubmit(async ({ first, last }) => {
        if (loading || first.line == null || last.line == null) return;

        const r = {
          hour: first.line.start.hours,
          min: first.line.start.minutes,
          sec: first.line.start.seconds,
          ms: Math.round(first.line.start.microseconds / 1000),
        };

        const scale = {
          from: totalMs(last.to) - totalMs(first.to),
          to: lineStartMs(last.line.start) - lineStartMs(first.line.start),
        };

        const action = convertToAction(r, first.to, scale);

        if (!isSource) {
          selections.forEach((s) => mutate({ action, form: s }));
          onSubmit?.();
          modals.closeSelf();
          return;
        }
        try {
          await Promise.all(
            selections.map((s) => mutateAsync({ action, form: s })),
          );
          onSubmit?.();
          modals.closeSelf();
        } catch (error) {
          if (!isShownConflict(error) && !isShownSubtitleActionError(error)) {
            showNotification(
              notification.error(
                "Alignment failed",
                "Unable to align subtitles",
              ),
            );
          }
        }
      })}
    >
      <LoadingOverlay
        visible={loading || (isSource && isPending)}
        zIndex={1000}
        overlayProps={{ radius: "sm", blur: 2 }}
      />
      <Stack>
        {isSource && query.error && (
          <Alert color="red" title="Subtitle unavailable">
            {query.error.message}
          </Alert>
        )}
        {isSource && !loading && !query.error && lines.length < 2 && (
          <Alert color="yellow">
            At least two subtitle sentences are needed.
          </Alert>
        )}
        <Alert>
          Select two sentences and the time for when they should appear. This
          will fit (offset and scale) every sentence.
        </Alert>
        {wrongOrder && (
          <Alert color="yellow" variant="filled">
            The first sentence appears after the last sentence in the subtitle
            file. Are the selections correct?
          </Alert>
        )}
        <Card>
          <Stack gap="md">
            <header>First sentence</header>
            <Alert variant="outline">
              The closer to the beginning, the better.
            </Alert>
            <Stack gap="0">
              <Selector
                {...options}
                disabled={query.isLoading}
                {...form.getInputProps("first.line")}
                onChange={(line: SubtitleContents.Line | null) => {
                  form.setFieldValue("first.line", line);
                  if (line) {
                    form.setFieldValue("first.to", lineStartToTime(line.start));
                  }
                }}
              ></Selector>
            </Stack>

            <Stack gap="0">
              <Group align="end" gap="xs" wrap="nowrap">
                <NumberInput
                  label="hour"
                  min={0}
                  {...form.getInputProps("first.to.hour")}
                ></NumberInput>
                <NumberInput
                  min={0}
                  label="min"
                  {...form.getInputProps("first.to.min")}
                ></NumberInput>
                <NumberInput
                  min={0}
                  label="sec"
                  {...form.getInputProps("first.to.sec")}
                ></NumberInput>
                <NumberInput
                  min={0}
                  label="ms"
                  {...form.getInputProps("first.to.ms")}
                ></NumberInput>
              </Group>
            </Stack>
          </Stack>
        </Card>

        <Card>
          <Stack gap="md">
            <header>Last sentence</header>
            <Alert variant="outline">The closer to the end, the better.</Alert>
            <Stack gap="0">
              <Selector
                {...options}
                disabled={query.isLoading}
                {...form.getInputProps("last.line")}
                onChange={(line: SubtitleContents.Line | null) => {
                  form.setFieldValue("last.line", line);
                  if (line) {
                    form.setFieldValue("last.to", lineStartToTime(line.start));
                  }
                }}
              ></Selector>
            </Stack>

            <Stack gap="0">
              <Group align="end" gap="xs" wrap="nowrap">
                <NumberInput
                  min={0}
                  label="hour"
                  {...form.getInputProps("last.to.hour")}
                ></NumberInput>
                <NumberInput
                  min={0}
                  label="min"
                  {...form.getInputProps("last.to.min")}
                ></NumberInput>
                <NumberInput
                  min={0}
                  label="sec"
                  {...form.getInputProps("last.to.sec")}
                ></NumberInput>
                <NumberInput
                  min={0}
                  label="ms"
                  {...form.getInputProps("last.to.ms")}
                ></NumberInput>
              </Group>
            </Stack>
          </Stack>
        </Card>

        <Divider></Divider>
        <Button
          disabled={
            loading || (isSource && (!!query.error || lines.length < 2))
          }
          loading={isSource && isPending}
          type="submit"
        >
          Align
        </Button>
      </Stack>
    </form>
  );
};

export const TwoPointFitModal = withModal(
  TwoPointFitForm,
  "two-point-alignment",
  {
    title: "Two-Point Fit",
  },
);

export default TwoPointFitForm;
