import {
  FunctionComponent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useBlocker, useNavigate, useParams } from "react-router";
import {
  Alert,
  Box,
  Button,
  Group,
  Modal,
  Paper,
  Stack,
  Text,
} from "@mantine/core";
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
import { StepBusyProvider, useStepBusy } from "./useStepBusy";
import { StepDraftProvider, useClearStepDrafts } from "./useStepDrafts";
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
  const clearStepDrafts = useClearStepDrafts();
  const stepBusy = useStepBusy();
  const navigate = useNavigate();
  const { stepKey } = useParams<{ stepKey?: string }>();
  const mutation = useSettingsMutation();
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);

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

  const { index, step: current, goTo, reset } = useWizardStep(steps);
  const StepComponent = current.Component;

  // The step key is in the URL, so the wizard has history of its own: browser
  // Back walks one step back instead of leaving the application, a reload
  // lands on the step it left, and a step can be linked to in a support
  // thread. The persisted cursor stays as the fallback for a bare /setup.
  const knownParam =
    stepKey !== undefined && steps.some((s) => s.key === stepKey);
  // The key of a move already asked for and not yet arrived. Without it the
  // two directions are indistinguishable: a stale URL beside a moved cursor
  // and a moved URL beside a stale cursor look exactly alike from here, and
  // whichever one this effect guessed at, it guessed wrong half the time.
  const pendingUrl = useRef<string | null>(null);
  // The steps behind the current entry that this wizard pushed, oldest first.
  // It is what lets Back walk the history that is already there instead of
  // writing more of it: pushing the earlier step on top made the next browser
  // Back go forwards, and replacing the current entry left the same step in
  // the two entries on top, so browser Back appeared to do nothing once.
  const trail = useRef<string[]>([]);

  // Moving the cursor and the URL is one action, so the effect below only ever
  // has to deal with a URL that moved on its own: a browser Back or Forward,
  // or a pasted link.
  const moveTo = useCallback(
    (key: string, from: string) => {
      pendingUrl.current = key;
      goTo(key);
      trail.current.push(from);
      navigate(`/setup/${key}`);
    },
    [goTo, navigate],
  );
  const next = useCallback(
    () => moveTo(steps[Math.min(index + 1, steps.length - 1)].key, current.key),
    [moveTo, steps, index, current.key],
  );
  const back = useCallback(() => {
    const target = steps[Math.max(index - 1, 0)].key;
    pendingUrl.current = target;
    // The cursor moves here rather than waiting on the history move, because
    // the step list is this component's, not the browser's.
    goTo(target);
    if (trail.current[trail.current.length - 1] === target) {
      // The entry underneath is the step being gone back to, so go back to it
      // rather than writing a third entry over the two that already say it.
      trail.current.pop();
      navigate(-1);
      return;
    }
    // The list changed under the history (a media server was unticked, say),
    // so the entry underneath is not this step. Replace rather than push: an
    // earlier step stacked on a later one turns browser Back into forward.
    navigate(`/setup/${target}`, { replace: true });
  }, [goTo, navigate, steps, index]);

  useEffect(() => {
    if (stepKey === current.key) {
      pendingUrl.current = null;
      return;
    }
    if (pendingUrl.current === current.key) {
      // Our own navigation, still on its way. It is also what keeps the wizard
      // working where there is no router to answer it at all.
      return;
    }
    if (knownParam && stepKey !== undefined) {
      pendingUrl.current = stepKey;
      if (trail.current[trail.current.length - 1] === stepKey) {
        // Backwards: the entry underneath is the one being moved to.
        trail.current.pop();
      } else {
        // Forwards, or a pasted link. Either way the entry being left is now
        // the one underneath, and forgetting that made the wizard's own Back
        // write a third entry over two that already said the same step.
        trail.current.push(current.key);
      }
      goTo(stepKey);
      return;
    }
    // The URL names no step, or names one this run does not walk. Neither is
    // an entry worth being able to go back to, so it is replaced.
    pendingUrl.current = current.key;
    navigate(`/setup/${current.key}`, { replace: true });
  }, [stepKey, knownParam, current.key, goTo, navigate]);

  // A step in the middle of work it has to finish is not left through the
  // address bar either. The skip is hidden for the same reason, and a browser
  // Back that unmounted the providers step left its installs finishing into
  // nothing, its restart arriving at nobody, and the poll that brings the
  // reader back armed after its own cleanup had run. The attempt is dropped
  // rather than queued: once the run is over, Back works again.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      stepBusy && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (blocker.state === "blocked" && !stepBusy) {
      blocker.reset?.();
    }
  }, [blocker, stepBusy]);

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

  // Marking setup complete ends onboarding, so it asks first. The write can
  // also fail, and a failed write used to produce nothing at all: no spinner,
  // no message, no navigation, just a button that had apparently done nothing.
  const handleLeave = () => {
    setLeaveError(null);
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          reset();
          resetIntent();
          clearSelection();
          clearStepDrafts();
          // The storage key is cleared here rather than left to the provider's
          // effect: the navigation below unmounts the provider in the same
          // commit, so the effect need not run, and the skipped drafts would
          // still be there to restore on the next visit to setup.
          clearPersistedSelection();
          setLeaving(false);
          // The Redirector picks routing back up once setup is marked complete.
          navigate("/");
        },
        onError: () =>
          setLeaveError(
            "Bazarr+ could not save that. You are still in setup, so nothing is lost. Check that Bazarr+ is reachable and try again.",
          ),
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
            <Button
              variant="subtle"
              color="gray"
              // Leaving ends the wizard, which takes the step with it, so it
              // waits for a step that is in the middle of work it has to
              // finish. An install left this way kept going into nothing and
              // restarted Bazarr+ at a reader who was already somewhere else.
              disabled={stepBusy}
              onClick={() => {
                setLeaveError(null);
                setLeaving(true);
              }}
            >
              Set up later
            </Button>
          </Group>
        </header>

        <Modal
          opened={leaving}
          // Dismissing does not cancel the write, so while one is in the air
          // there is nothing to dismiss to: pressing Keep going and then
          // landing on the home page anyway is the answer the reader did not
          // give.
          onClose={() => {
            if (!mutation.isPending) {
              setLeaving(false);
            }
          }}
          closeOnClickOutside={!mutation.isPending}
          closeOnEscape={!mutation.isPending}
          withCloseButton={!mutation.isPending}
          title="Leave setup?"
          centered
        >
          <Stack gap="md">
            <Text size="sm">
              You can run first-time setup again any time from Settings,
              General. Anything you have already saved stays saved.
            </Text>
            {leaveError && (
              <Alert color="red" title="Could not leave setup">
                {leaveError}
              </Alert>
            )}
            <Group justify="flex-end">
              <Button
                variant="default"
                onClick={() => setLeaving(false)}
                disabled={mutation.isPending}
              >
                Keep going
              </Button>
              <Button
                color="red"
                onClick={handleLeave}
                loading={mutation.isPending}
              >
                Leave setup
              </Button>
            </Group>
          </Stack>
        </Modal>

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
            stepKey={current.key}
          />
        </Paper>

        {current.optional && !stepBusy ? (
          <div className={styles.stepSkip}>
            <Button variant="subtle" color="gray" onClick={next}>
              {current.skipLabel ?? "Do this later"}
            </Button>
          </div>
        ) : (
          !current.optional &&
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
      <StepDraftProvider>
        <StepBusyProvider>
          <OnboardingWizardBody />
        </StepBusyProvider>
      </StepDraftProvider>
    </OnboardingSelectionProvider>
  </OnboardingIntentProvider>
);

export default OnboardingWizardView;
