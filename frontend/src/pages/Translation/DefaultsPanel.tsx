/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import {
  Alert,
  Button,
  Group,
  NumberInput,
  Skeleton,
  Switch,
} from "@mantine/core";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import {
  afterStateDelivered,
  readRefusal,
  useSaveLimits,
} from "@/apis/hooks/translation";
import type { TranslationSummary } from "@/types/translation";
import styles from "./Translation.module.scss";

/**
 * A number field's value as the field reports it. It is a number once what is typed is a whole number,
 * and the typed text until then ("0." on the way to "0.15", "1.50", an empty field), so it is kept as
 * it comes: turning the text into something else would rewrite the field under the person's hands.
 */
type Typed = number | string;

interface Draft {
  default_score?: Typed;
  min_source_score?: Typed;
  translator_info?: boolean;
  max_concurrent?: Typed;
  job_cap?: Typed;
  month_cap?: Typed;
}

/** What a field holds, as a number: null when it is empty, NaN when the text is not a number. */
const amount = (value: Typed): number | null => {
  if (typeof value === "number") {
    return value;
  }
  const text = value.trim();
  if (text === "") {
    return null;
  }
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
};

/** Why a whole-number field cannot be saved as it stands, or nothing when it can or was not touched. */
const wholeNumberProblem = (
  value: Typed | undefined,
  min: number,
  max: number,
  what: string,
) => {
  if (value === undefined) {
    return undefined;
  }
  const number = amount(value);
  if (number === null || Number.isNaN(number)) {
    return `Enter a ${what}.`;
  }
  // The field brings a number back into its range when it is left. Enter saves without leaving it.
  return Number.isInteger(number) && number >= min && number <= max
    ? undefined
    : `Enter a ${what} from ${min} to ${max}.`;
};

/** A cap is an amount of 0 or more, or empty for none. */
const capProblem = (value: Typed | undefined) => {
  if (value === undefined) {
    return undefined;
  }
  const number = amount(value);
  return number === null || number >= 0
    ? undefined
    : "Enter an amount, or leave it empty for no cap.";
};

const cap = (value: Typed | undefined, saved: number | null) =>
  value === undefined ? saved : amount(value);

/**
 * What applies to every translation, whatever engine runs it: the score a
 * translated subtitle gets, the score a source needs, the credit line, how
 * many run at once and what they may cost. The caps are shown only when the
 * server reports them.
 */
const DefaultsPanel: FunctionComponent<{ summary?: TranslationSummary }> = ({
  summary,
}) => {
  const settings = useSystemSettings();
  const saveSettings = useSettingsMutation(false, { inlineErrors: true });
  const saveLimits = useSaveLimits();
  // Only what was changed. A field left alone follows what is saved.
  const [draft, setDraft] = useState<Draft>({});
  const [problem, setProblem] = useState("");
  // The saved values are being loaded back, to confirm what the fields will show.
  const [confirming, setConfirming] = useState(false);
  const stored = settings.data?.translator;

  if (settings.isLoading && !stored) {
    return (
      <div className={styles.defaults} aria-busy="true">
        <Skeleton height={56} radius="md" />
        <Skeleton height={56} radius="md" />
        <Skeleton height={56} radius="md" />
      </div>
    );
  }
  if (!stored) {
    return (
      <Alert color="red" role="alert" maw={640}>
        The defaults could not be loaded.{" "}
        <Button
          size="compact-xs"
          variant="default"
          ml="xs"
          onClick={() => void settings.refetch()}
        >
          Try again
        </Button>
      </Alert>
    );
  }

  const set = (patch: Draft) => {
    setProblem("");
    setDraft((current) => ({ ...current, ...patch }));
  };
  const dirty = Object.keys(draft).length > 0;
  const busy = saveSettings.isPending || saveLimits.isPending || confirming;
  const problems = {
    default_score: wholeNumberProblem(draft.default_score, 0, 100, "score"),
    min_source_score: wholeNumberProblem(
      draft.min_source_score,
      0,
      100,
      "score",
    ),
    max_concurrent: wholeNumberProblem(draft.max_concurrent, 1, 10, "number"),
    job_cap: summary ? capProblem(draft.job_cap) : undefined,
    month_cap: summary ? capProblem(draft.month_cap) : undefined,
  };
  const invalid = Object.values(problems).some(Boolean);

  const save = async () => {
    const changes: Record<string, number | boolean> = {};
    // Checked above: a field that was touched holds a whole number in its range.
    const whole = (value: Typed | undefined) =>
      value === undefined ? null : amount(value);
    const defaultScore = whole(draft.default_score);
    if (defaultScore !== null) {
      changes["settings-translator-default_score"] = defaultScore;
    }
    const minSourceScore = whole(draft.min_source_score);
    if (minSourceScore !== null) {
      changes["settings-translator-min_source_score"] = minSourceScore;
    }
    if (draft.translator_info !== undefined) {
      changes["settings-translator-translator_info"] = draft.translator_info;
    }
    const maxConcurrent = whole(draft.max_concurrent);
    if (maxConcurrent !== null) {
      changes["settings-translator-openrouter_max_concurrent"] = maxConcurrent;
    }
    // A new attempt starts clean: what is said below belongs to this one.
    setProblem("");
    const settingsChanged = Object.keys(changes).length > 0;
    try {
      if (settingsChanged) {
        await saveSettings.mutateAsync(changes);
      }
    } catch (error) {
      setProblem(
        readRefusal(error)?.message ||
          "The defaults were not saved. Try again.",
      );
      return;
    }
    if (
      summary &&
      (draft.job_cap !== undefined || draft.month_cap !== undefined)
    ) {
      try {
        await saveLimits.mutateAsync({
          job_cap: cap(draft.job_cap, summary.job_cap),
          month_cap: cap(draft.month_cap, summary.month.cap),
        });
      } catch (error) {
        // The two are separate writes: say which one did not go through.
        const reason = readRefusal(error)?.message;
        setProblem(
          `${settingsChanged ? "Your other changes were saved. " : ""}The cost caps were not saved${
            reason ? `: ${reason}` : ". Try again."
          }`,
        );
        return;
      }
    }
    // Saved. The fields keep what was sent until the saved values have been loaded back: dropping the
    // draft earlier would show the old values for a moment, or for good when the load fails.
    setConfirming(true);
    try {
      const loaded =
        Object.keys(changes).length > 0 ? await settings.refetch() : null;
      if (loaded && (loaded.isError || loaded.data === undefined)) {
        setProblem(
          "Your changes were saved, but the saved values could not be loaded back. The fields show what you sent.",
        );
        return;
      }
      afterStateDelivered(() => setDraft({}));
    } finally {
      setConfirming(false);
    }
  };

  return (
    <form
      className={styles.defaults}
      onSubmit={(event) => {
        event.preventDefault();
        if (dirty && !invalid) {
          void save();
        }
      }}
    >
      <section className={styles.fieldGroup} aria-labelledby="defaults-scores">
        <div>
          <h2 className={styles.sectionTitle} id="defaults-scores">
            Scores
          </h2>
          <p className={styles.sectionHint}>
            Scores decide which subtitle wins when there is more than one.
          </p>
        </div>
        <div className={styles.fieldPair}>
          <NumberInput
            label="Score of a translated subtitle"
            description="From 0 to 100. A found subtitle that scores higher replaces it."
            min={0}
            max={100}
            allowDecimal={false}
            value={draft.default_score ?? stored.default_score}
            error={problems.default_score}
            disabled={busy}
            onChange={(value) => set({ default_score: value })}
          />
          <NumberInput
            label="Minimum source score"
            description="A subtitle must score at least this to be translated from."
            min={0}
            max={100}
            allowDecimal={false}
            value={draft.min_source_score ?? stored.min_source_score ?? 90}
            error={problems.min_source_score}
            disabled={busy}
            onChange={(value) => set({ min_source_score: value })}
          />
        </div>
      </section>

      <section className={styles.fieldGroup} aria-labelledby="defaults-output">
        <h2 className={styles.sectionTitle} id="defaults-output">
          Output
        </h2>
        <Switch
          label="Add a credit line"
          description="A short line at the end of a translated subtitle that names the translator."
          checked={draft.translator_info ?? stored.translator_info}
          disabled={busy}
          onChange={(event) =>
            set({ translator_info: event.currentTarget.checked })
          }
        />
      </section>

      <section className={styles.fieldGroup} aria-labelledby="defaults-limits">
        <div>
          <h2 className={styles.sectionTitle} id="defaults-limits">
            Limits
          </h2>
          {summary && (
            <p className={styles.sectionHint}>
              A cap stops paid back ends. Free ones keep translating after the
              month is spent.
            </p>
          )}
        </div>
        <NumberInput
          label="Translations at once"
          description="From 1 to 10. The rest wait in the queue."
          min={1}
          max={10}
          allowDecimal={false}
          maw={summary ? undefined : 313}
          value={draft.max_concurrent ?? stored.openrouter_max_concurrent ?? 2}
          error={problems.max_concurrent}
          disabled={busy}
          onChange={(value) => set({ max_concurrent: value })}
        />
        {summary && (
          <div className={styles.fieldPair}>
            <NumberInput
              label="Cost cap per job"
              description="In dollars. Empty for no cap."
              min={0}
              decimalScale={2}
              prefix="$"
              placeholder="No cap"
              value={
                draft.job_cap === undefined
                  ? (summary.job_cap ?? "")
                  : draft.job_cap
              }
              error={problems.job_cap}
              disabled={busy}
              onChange={(value) => set({ job_cap: value })}
            />
            <NumberInput
              label="Cost cap per month"
              description="In dollars. Empty for no cap."
              min={0}
              decimalScale={2}
              prefix="$"
              placeholder="No cap"
              value={
                draft.month_cap === undefined
                  ? (summary.month.cap ?? "")
                  : draft.month_cap
              }
              error={problems.month_cap}
              disabled={busy}
              onChange={(value) => set({ month_cap: value })}
            />
          </div>
        )}
      </section>

      {problem && (
        <Alert color="red" role="alert">
          {problem}
        </Alert>
      )}
      <Group gap="xs">
        <Button type="submit" loading={busy} disabled={!dirty || invalid}>
          Save defaults
        </Button>
        {dirty && (
          <Button
            variant="default"
            disabled={busy}
            onClick={() => {
              setProblem("");
              setDraft({});
            }}
          >
            Discard changes
          </Button>
        )}
      </Group>
    </form>
  );
};

export default DefaultsPanel;
