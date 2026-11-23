import { FunctionComponent } from "react";
import { Button, Divider, NumberInput, Stack, Text } from "@mantine/core";
import { useForm } from "@mantine/form";
import { useSubtitleScoreOverride } from "@/apis/hooks";
import { useModals, withModal } from "@/modules/modals";
import FormUtils from "@/utilities/form";

interface Props {
  selections: FormType.ModifySubtitle[];
  onSubmit?: VoidFunction;
}

const ScoreOverrideForm: FunctionComponent<Props> = ({
  selections,
  onSubmit,
}) => {
  const { mutate } = useSubtitleScoreOverride();
  const modals = useModals();

  const form = useForm({
    initialValues: {
      score: 100,
    },
    validate: {
      score: FormUtils.validation(
        (value: number | string) =>
          typeof value === "number" &&
          Number.isInteger(value) &&
          value >= 0 &&
          value <= 100,
        "The score must be a whole number from 0 to 100",
      ),
    },
  });

  return (
    <form
      onSubmit={form.onSubmit(({ score }) => {
        selections.forEach((s) => {
          if (!s.path) return;
          mutate({
            type: s.type,
            id: s.id,
            path: s.path,
            score,
            // eslint-disable-next-line camelcase
            arr_instance_id: s.arr_instance_id,
          });
        });

        onSubmit?.();
        modals.closeSelf();
      })}
    >
      <Stack>
        <Text size="sm" c="dimmed">
          Replaces the score of this subtitle file. A later download of the file
          releases it.
        </Text>
        <NumberInput
          label="Score (%)"
          min={0}
          max={100}
          step={1}
          allowDecimal={false}
          allowNegative={false}
          {...form.getInputProps("score")}
        ></NumberInput>
        <Divider></Divider>
        <Button type="submit">Set score</Button>
      </Stack>
    </form>
  );
};

export const ScoreOverrideModal = withModal(
  ScoreOverrideForm,
  "score-override-tool",
  {
    title: "Set Subtitle Score",
  },
);

export default ScoreOverrideForm;
