import { FunctionComponent, useMemo } from "react";
import { useNavigate } from "react-router";
import { Box, Button, Group, Paper, Text } from "@mantine/core";
import { faCheck } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSettingsMutation } from "@/apis/hooks";
import { PHASE_LABELS, WIZARD_PHASES } from "./steps/types";
import { buildSteps } from "./steps";
import {
  OnboardingIntentProvider,
  useOnboardingIntent,
} from "./useOnboardingIntent";
import type { MediaServerDraftSummary } from "./useOnboardingSelection";
import {
  clearPersistedSelection,
  OnboardingSelectionProvider,
  useOnboardingSelection,
} from "./useOnboardingSelection";
import { useWizardStep } from "./useWizardStep";
import styles from "./OnboardingWizard.module.scss";

/**
 * Full-screen first-run wizard. Rendered as a sibling of the app chrome (no
 * nav), it walks a fresh install through setup.
 *
 * The list is built from the intent answered on the second screen and the media
 * servers ticked on the picker, so it grows and shrinks while the reader walks
 * it. The cursor is therefore a step key, not an index into a list that
 * renumbers underneath it.
 *
 * The shell owns skipping: one control, one label, rendered from step.optional,
 * so a step never has to invent its own. A step that cannot be skipped says why
 * in one line instead of offering a button that does nothing useful.
 */
const OnboardingWizardBody: FunctionComponent = () => {
  const { intent, resetIntent } = useOnboardingIntent();
  const { drafts, clearSelection } = useOnboardingSelection();
  const navigate = useNavigate();
  const mutation = useSettingsMutation();

  const signature = drafts
    .map((draft) => `${draft.draftId}:${draft.kind}:${draft.instanceId ?? ""}`)
    .join("|");
  const mediaServers = useMemo<MediaServerDraftSummary[]>(
    () =>
      drafts.map(({ draftId, kind, instanceId }) => ({
        draftId,
        kind,
        instanceId,
      })),
    // Keyed on the signature rather than the array. A draft's identity changes
    // on every keystroke in its form, and a rebuilt list means rebuilt step
    // components, which would remount the form the reader is typing in.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [signature],
  );
  const steps = useMemo(
    () => buildSteps({ intent, mediaServers }),
    [intent, mediaServers],
  );

  const { index, step: current, next, back, reset } = useWizardStep(steps);
  const StepComponent = current.Component;

  // Progress is reported per phase, never as a step total. Before the intent
  // is answered nobody knows which path runs, and the media server segment
  // grows as servers are ticked, so a denominator over the whole wizard would
  // change under the reader. The phases do not, and the generated segment
  // counts itself.
  const phaseIndex = WIZARD_PHASES.indexOf(current.phase);
  const inPhase = steps.filter((s) => s.phase === current.phase);
  const inSegment = current.segment
    ? steps.filter((s) => s.segment === current.segment)
    : [];
  const showCount = intent !== null || current.phase === "start";
  const counter =
    inSegment.length > 0
      ? `${current.segment} ${inSegment.findIndex((s) => s.key === current.key) + 1} of ${inSegment.length}`
      : `${inPhase.findIndex((s) => s.key === current.key) + 1} of ${inPhase.length}`;

  const handleSkip = () => {
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          reset();
          resetIntent();
          clearSelection();
          // The storage key is cleared here rather than left to the provider's
          // effect: the navigation below unmounts the provider in the same
          // commit, so the effect need not run, and the skipped drafts would
          // still be there to restore on the next visit to setup.
          clearPersistedSelection();
          // The Redirector picks routing back up once setup is marked complete.
          navigate("/");
        },
      },
    );
  };

  return (
    <Box className={styles.root}>
      <div className={styles.shell}>
        <header className={styles.header}>
          <div className={styles.brand}>
            <span className={styles.brandTitle}>Bazarr+ Setup</span>
            <Text className={styles.progress}>
              {PHASE_LABELS[current.phase]}
              {showCount ? ` · ${counter}` : ""}
            </Text>
          </div>
          <Group gap="sm">
            <Button variant="subtle" color="gray" onClick={handleSkip}>
              Skip setup
            </Button>
          </Group>
        </header>

        <div className={styles.rail} aria-hidden>
          {WIZARD_PHASES.map((phase, position) => {
            const done = position < phaseIndex;
            const active = position === phaseIndex;
            const dotClass = [
              styles.dot,
              done ? styles.dotDone : "",
              active ? styles.dotActive : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <div key={phase} className={styles.railNode}>
                <div className={styles.railLabelGroup}>
                  <span className={dotClass} title={PHASE_LABELS[phase]}>
                    {done ? <FontAwesomeIcon icon={faCheck} /> : position + 1}
                  </span>
                  <span
                    className={[
                      styles.railLabel,
                      active ? styles.railLabelActive : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    {PHASE_LABELS[phase]}
                  </span>
                </div>
                {position < WIZARD_PHASES.length - 1 && (
                  <span
                    className={[
                      styles.connector,
                      done ? styles.connectorDone : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  />
                )}
              </div>
            );
          })}
        </div>

        <Paper className={styles.card}>
          {/* Keyed by the step, so moving to another step starts its component
              clean while a rebuild of the list around the current one does
              not: the media server steps all share one component type now, and
              without a key React would carry one server's form state into the
              next server's screen. */}
          <StepComponent
            key={current.key}
            onNext={next}
            onBack={index > 0 ? back : undefined}
            draftId={current.draftId}
          />
        </Paper>

        {current.optional ? (
          <div className={styles.stepSkip}>
            <Button variant="subtle" color="gray" onClick={next}>
              Skip this step
            </Button>
          </div>
        ) : (
          current.requiredReason && (
            <Text className={styles.stepNote}>{current.requiredReason}</Text>
          )
        )}
      </div>
    </Box>
  );
};

const OnboardingWizardView: FunctionComponent = () => (
  <OnboardingIntentProvider>
    <OnboardingSelectionProvider>
      <OnboardingWizardBody />
    </OnboardingSelectionProvider>
  </OnboardingIntentProvider>
);

export default OnboardingWizardView;
