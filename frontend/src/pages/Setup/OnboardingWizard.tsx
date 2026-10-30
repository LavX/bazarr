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
  VisuallyHidden,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faCheck } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQueryClient } from "@tanstack/react-query";
import { useSettingsMutation } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { PHASE_LABELS, WIZARD_PHASES } from "./steps/types";
import { clearPersistedConnectionTests } from "./connectionTests";
import { settleSetupComplete } from "./setupCompleteCache";
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
import { clearPersistedSkips, useSkippedSteps } from "./useSkippedSteps";
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
  const {
    skipped,
    markSkipped,
    markAnswered,
    reset: resetSkipped,
  } = useSkippedSteps();
  const navigate = useNavigate();
  const { stepKey } = useParams<{ stepKey?: string }>();
  const mutation = useSettingsMutation(false, { inlineErrors: true });
  const client = useQueryClient();
  const [leaving, setLeaving] = useState(false);
  const [leaveError, setLeaveError] = useState<string | null>(null);
  // The write answers before the cache has settled, and the navigation waits
  // for the settle. TanStack does not count a mutate callback's own work as
  // pending, so without this the modal went live again for that second wait.
  const [settling, setSettling] = useState(false);
  const leavePending = mutation.isPending || settling;

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
  // What the coarse-pointer touch floor in OnboardingWizard.module.scss is
  // scoped to. It sits on the document rather than on .root because the
  // dialogs and dropdowns a step opens are portalled out of the shell.
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-bz-setup", "");
    return () => root.removeAttribute("data-bz-setup");
  }, []);
  const instanceName = useInstanceName()?.trim() || "Bazarr+";
  useDocumentTitle(`${current.label} - Setup - ${instanceName}`);
  const mainRef = useRef<HTMLElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  // What the status region last said. Empty until the reader first moves, so
  // nothing is read out over the page a screen reader announces on load.
  const [announcement, setAnnouncement] = useState("");

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
  // Whether the URL and the cursor have agreed at least once. Until they have,
  // a URL that names another step is a link somebody opened, not a move
  // through this wizard, and the step it resolved from was never an entry in
  // the browser's history to go back to.
  const synced = useRef(false);
  // The steps behind the current entry that this wizard pushed, oldest first.
  // It is what lets Back walk the history that is already there instead of
  // writing more of it: pushing the earlier step on top made the next browser
  // Back go forwards, and replacing the current entry left the same step in
  // the two entries on top, so browser Back appeared to do nothing once.
  const trail = useRef<string[]>([]);
  // Leaving setup is final, and the effect below is what used to undo it. The
  // reset clears the cursor, but this component keeps rendering until the
  // navigation unmounts it, and on that render the URL still names the step
  // being left while the list still knows it: the effect read that as a link
  // somebody followed and wrote the cursor straight back. "Run first-time
  // setup" then reopened on that step with the rail showing a phase nobody had
  // reached. Once this is set, neither the URL nor the cursor is worth
  // correcting any more.
  const left = useRef(false);

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
    // Entries on top of the trail that name a step this run no longer walks (a
    // media server's configure step is gone the moment its save lands) sit
    // between here and the step being gone back to. Landing on one moves
    // nothing the reader can see, so they are gone over in the same move.
    let passedOver = 0;
    let top = trail.current.length - 1;
    while (top >= 0 && !steps.some((s) => s.key === trail.current[top])) {
      passedOver += 1;
      top -= 1;
    }
    if (top >= 0 && trail.current[top] === target) {
      // The entry underneath is the step being gone back to, so go back to it
      // rather than writing a third entry over the two that already say it.
      trail.current.length = top;
      navigate(-(passedOver + 1));
      return;
    }
    // The list changed under the history (a media server was unticked, say),
    // so the entry underneath is not this step. Replace rather than push: an
    // earlier step stacked on a later one turns browser Back into forward.
    navigate(`/setup/${target}`, { replace: true });
  }, [goTo, navigate, steps, index]);

  // What a step is handed to move the wizard with. A step's write can answer
  // after the reader has already left it (browser Back is blocked only for a
  // step that reports itself busy, so any other step is unmounted with its save
  // in the air), and the callback it holds then advanced the wizard one step on
  // from wherever the reader had gone. The callbacks are
  // tied to the mount they were handed to and do nothing once it is no longer
  // the one on screen. A step key would not do for that: Back and then Forward
  // before the old save lands puts the same key on screen again, as a fresh
  // mount, and the old save would advance it. Each visit gets the next number
  // instead. The callbacks also call the latest move rather than the one in
  // force when they were made, so a list that changed in between is not
  // walked from a stale index.
  const visit = useRef({ key: current.key, id: 0 });
  if (visit.current.key !== current.key) {
    visit.current = { key: current.key, id: visit.current.id + 1 };
  }
  const shownVisit = useRef(visit.current.id);
  shownVisit.current = visit.current.id;
  const moves = useRef({ next, back });
  moves.current = { next, back };
  // The list and cursor as they stand now, for the history effect, which must
  // not run again every time the list changes.
  const latest = useRef({ steps, index });
  latest.current = { steps, index };
  const movesFor = useCallback(
    (forStep: string, forVisit: number) => ({
      next: () => {
        if (shownVisit.current === forVisit) {
          // The step's own Continue is an answer, whatever it wrote, so an
          // earlier skip of this step no longer stands.
          markAnswered(forStep);
          moves.current.next();
        }
      },
      back: () => {
        if (shownVisit.current === forVisit) {
          moves.current.back();
        }
      },
    }),
    [markAnswered],
  );

  useEffect(() => {
    if (left.current) {
      return;
    }
    if (stepKey === current.key) {
      pendingUrl.current = null;
      synced.current = true;
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
      } else if (synced.current) {
        // Forwards, or a link followed from inside the wizard. Either way the
        // step being left is now the entry underneath, and forgetting that
        // made the wizard's own Back write a third entry over two that
        // already said the same step. A link opened before the two ever
        // agreed is not that: the entry underneath it belongs to whatever the
        // reader was looking at, which is not a step of this wizard.
        trail.current.push(current.key);
      }
      goTo(stepKey);
      return;
    }
    if (
      stepKey !== undefined &&
      trail.current[trail.current.length - 1] === stepKey
    ) {
      // An entry this wizard pushed for a step that has since gone. Replacing
      // it with the current step left the press looking like it did nothing,
      // so it is passed over: the entry beneath it is the one to land on.
      trail.current.pop();
      if (trail.current.length > 0) {
        navigate(-1);
        return;
      }
      // Nothing this wizard pushed is beneath it: the run was entered at the
      // step that has gone (a reload, or a link) and the entry underneath is
      // whatever the reader was on before. Passing over it would leave the
      // wizard altogether on a press of Back, so the entry is replaced with
      // the live step just before the cursor instead.
      const { steps: live, index: at } = latest.current;
      const target = live[Math.max(at - 1, 0)].key;
      pendingUrl.current = target;
      goTo(target);
      navigate(`/setup/${target}`, { replace: true });
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
    ({ currentLocation, nextLocation, historyAction }) =>
      // Only a history move, which is the reader leaving through the back or
      // forward button. The wizard's own moves are pushes and replacements,
      // and blocking those would stop the busy step from finishing its work:
      // a provider save ends by advancing the wizard itself.
      historyAction === "POP" &&
      stepBusy &&
      currentLocation.pathname !== nextLocation.pathname,
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
  const progressText = `${PHASE_LABELS[current.phase]}${showCount ? `, ${counter}` : ""}`;
  // A phase behind the reader is completed unless everything in it was walked
  // past with the skip control. It is then visited, which the rail draws as
  // its number rather than a check: four skipped steps used to show four.
  const skippedPhases = WIZARD_PHASES.filter((phase, position) => {
    const inIt = steps.filter((s) => s.phase === phase);
    return (
      position < phaseIndex &&
      inIt.length > 0 &&
      inIt.every((s) => skipped.includes(s.key))
    );
  });

  // A step change swaps the whole card, and the control that was pressed goes
  // with it, so focus fell to the body and a keyboard reader started again from
  // the top of the document on every step. Focus goes to the new step's
  // heading instead, and the status region says where the reader now is. The
  // key the reader last saw is kept rather than a first-render flag: React
  // runs this twice on mount in strict mode, and only a real move should take
  // focus.
  const shownKey = useRef(current.key);
  // A step that loads before it draws (Providers waits on its catalog) has
  // no heading yet when it arrives. Focus waits on main, and this hands it to
  // the heading the moment one is drawn, unless the reader has moved it
  // somewhere else first.
  const awaitHeading = useRef<MutationObserver | null>(null);
  useEffect(() => () => awaitHeading.current?.disconnect(), []);
  useEffect(() => {
    if (shownKey.current === current.key) {
      return;
    }
    shownKey.current = current.key;
    awaitHeading.current?.disconnect();
    awaitHeading.current = null;
    const card = cardRef.current;
    const findHeading = () =>
      card?.querySelector<HTMLElement>("[data-step-heading]") ?? null;
    const heading = findHeading();
    if (heading !== null) {
      heading.focus();
    } else {
      mainRef.current?.focus();
      if (card) {
        const observer = new MutationObserver(() => {
          const drawn = findHeading();
          if (drawn === null) {
            return;
          }
          observer.disconnect();
          awaitHeading.current = null;
          const active = document.activeElement;
          if (active === mainRef.current || active === document.body) {
            drawn.focus();
          }
        });
        observer.observe(card, { childList: true, subtree: true });
        awaitHeading.current = observer;
      }
    }
    setAnnouncement(`${current.label}. ${progressText}`);
  }, [current.key, current.label, progressText]);

  // Marking setup complete ends onboarding, so it asks first. The write can
  // also fail, and a failed write used to produce nothing at all: no spinner,
  // no message, no navigation, just a button that had apparently done nothing.
  const handleLeave = () => {
    if (leavePending) {
      return;
    }
    setLeaveError(null);
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: async () => {
          // Never cleared: the navigation below unmounts the wizard, and the
          // settle does not reject.
          setSettling(true);
          await settleSetupComplete(client, true);
          left.current = true;
          reset();
          resetIntent();
          clearSelection();
          clearStepDrafts();
          // The storage key is cleared here rather than left to the provider's
          // effect: the navigation below unmounts the provider in the same
          // commit, so the effect need not run, and the skipped drafts would
          // still be there to restore on the next visit to setup.
          clearPersistedSelection();
          clearPersistedConnectionTests();
          resetSkipped();
          clearPersistedSkips();
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
            <h1 className={styles.brandTitle}>Bazarr+ Setup</h1>
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
          // give. The same holds until the cache has settled and the
          // navigation has gone.
          onClose={() => {
            if (!leavePending) {
              setLeaving(false);
            }
          }}
          closeOnClickOutside={!leavePending}
          closeOnEscape={!leavePending}
          withCloseButton={!leavePending}
          // Mantine draws the close button as an icon with no name, and the
          // measured pass found it announced as an unnamed button.
          closeButtonProps={{ "aria-label": "Close" }}
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
                disabled={leavePending}
              >
                Keep going
              </Button>
              <Button color="red" onClick={handleLeave} loading={leavePending}>
                Leave setup
              </Button>
            </Group>
          </Stack>
        </Modal>

        <main ref={mainRef} className={styles.main} tabIndex={-1}>
          {/* A progressbar's children are presentational, so the dots and their
            labels are drawn for the eye and the value text carries all of it
            for everyone else. The value counts the phases already done, from
            0: Orca ignores the value text and reads value / (max - min) as a
            percentage, which from a minimum of 1 told the first screen it was
            33 percent through. */}
          <div
            className={styles.rail}
            role="progressbar"
            aria-label="Setup progress"
            aria-valuemin={0}
            aria-valuemax={WIZARD_PHASES.length}
            aria-valuenow={phaseIndex}
            aria-valuetext={`Part ${phaseIndex + 1} of ${WIZARD_PHASES.length}: ${progressText}${
              skippedPhases.length > 0
                ? `, skipped: ${skippedPhases.map((phase) => PHASE_LABELS[phase]).join(", ")}`
                : ""
            }`}
          >
            {WIZARD_PHASES.map((phase, position) => {
              const passed = position < phaseIndex;
              const skippedPhase = skippedPhases.includes(phase);
              const done = passed && !skippedPhase;
              const active = position === phaseIndex;
              const dotClass = [
                styles.dot,
                done ? styles.dotDone : "",
                skippedPhase ? styles.dotSkipped : "",
                active ? styles.dotActive : "",
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <div key={phase} className={styles.railNode}>
                  <div className={styles.railLabelGroup}>
                    <span className={dotClass}>
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
                        passed ? styles.connectorDone : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                    />
                  )}
                </div>
              );
            })}
          </div>

          {/* A polite live region, not role="status": Orca files a status bar
            under its own UI text and never reads its changes out, which an
            Orca pass on this wizard showed. */}
          <VisuallyHidden
            aria-live="polite"
            aria-atomic="true"
            data-testid="step-announcement"
          >
            {announcement}
          </VisuallyHidden>

          <Paper ref={cardRef} className={styles.card}>
            {/* Keyed by the step, so moving to another step starts its component
              clean while a rebuild of the list around the current one does
              not: the media server steps all share one component type now, and
              without a key React would carry one server's form state into the
              next server's screen. */}
            <StepComponent
              key={current.key}
              onNext={movesFor(current.key, visit.current.id).next}
              // No Back while the step is finishing work either. Back moves the
              // cursor itself, which unmounts the step before any history guard
              // can see it, and an install that loses its screen still restarts
              // Bazarr+ at a reader who is no longer there.
              onBack={
                index > 0 && !stepBusy
                  ? movesFor(current.key, visit.current.id).back
                  : undefined
              }
              draftId={current.draftId}
              stepKey={current.key}
            />
          </Paper>

          {current.optional && !stepBusy ? (
            <div className={styles.stepSkip}>
              <Button
                variant="subtle"
                color="gray"
                onClick={() => {
                  markSkipped(current.key);
                  next();
                }}
              >
                {current.skipLabel ?? "Do this later"}
              </Button>
            </div>
          ) : (
            !current.optional &&
            current.requiredReason && (
              <Text className={styles.stepNote}>{current.requiredReason}</Text>
            )
          )}
        </main>
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
