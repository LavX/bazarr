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
import { readRefusal, useSaveLimits } from "@/apis/hooks/translation";
import type { TranslationSummary } from "@/types/translation";
import styles from "./Translation.module.scss";

interface Draft {
  default_score?: number | "";
  min_source_score?: number | "";
  translator_info?: boolean;
  max_concurrent?: number | "";
  job_cap?: number | "";
  month_cap?: number | "";
}

const asNumber = (value: string | number): number | "" =>
  typeof value === "number" ? value : "";

const cap = (value: number | "" | undefined, saved: number | null) =>
  value === undefined ? saved : value === "" ? null : value;

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
  const busy = saveSettings.isPending || saveLimits.isPending;
  const invalid =
    draft.default_score === "" ||
    draft.min_source_score === "" ||
    draft.max_concurrent === "";

  const save = async () => {
    const changes: Record<string, number | boolean> = {};
    if (typeof draft.default_score === "number") {
      changes["settings-translator-default_score"] = draft.default_score;
    }
    if (typeof draft.min_source_score === "number") {
      changes["settings-translator-min_source_score"] = draft.min_source_score;
    }
    if (draft.translator_info !== undefined) {
      changes["settings-translator-translator_info"] = draft.translator_info;
    }
    if (typeof draft.max_concurrent === "number") {
      changes["settings-translator-openrouter_max_concurrent"] =
        draft.max_concurrent;
    }
    try {
      if (Object.keys(changes).length > 0) {
        await saveSettings.mutateAsync(changes);
      }
      if (
        summary &&
        (draft.job_cap !== undefined || draft.month_cap !== undefined)
      ) {
        await saveLimits.mutateAsync({
          job_cap: cap(draft.job_cap, summary.job_cap),
          month_cap: cap(draft.month_cap, summary.month.cap),
        });
      }
      setDraft({});
    } catch (error) {
      setProblem(
        readRefusal(error)?.message ||
          "The defaults were not saved. Try again.",
      );
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
            error={draft.default_score === "" ? "Enter a score." : undefined}
            disabled={busy}
            onChange={(value) => set({ default_score: asNumber(value) })}
          />
          <NumberInput
            label="Minimum source score"
            description="A subtitle must score at least this to be translated from."
            min={0}
            max={100}
            allowDecimal={false}
            value={draft.min_source_score ?? stored.min_source_score ?? 90}
            error={draft.min_source_score === "" ? "Enter a score." : undefined}
            disabled={busy}
            onChange={(value) => set({ min_source_score: asNumber(value) })}
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
          error={draft.max_concurrent === "" ? "Enter a number." : undefined}
          disabled={busy}
          onChange={(value) => set({ max_concurrent: asNumber(value) })}
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
              disabled={busy}
              onChange={(value) => set({ job_cap: asNumber(value) })}
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
              disabled={busy}
              onChange={(value) => set({ month_cap: asNumber(value) })}
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
