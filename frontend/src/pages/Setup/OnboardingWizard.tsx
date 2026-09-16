import { FunctionComponent, useMemo } from "react";
import { useNavigate } from "react-router";
import { Box, Button, Group, Paper, Text } from "@mantine/core";
import { faCheck } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSettingsMutation } from "@/apis/hooks";
import { stepsForIntent } from "./steps";
import {
  OnboardingIntentProvider,
  useOnboardingIntent,
} from "./useOnboardingIntent";
import { useWizardStep } from "./useWizardStep";
import styles from "./OnboardingWizard.module.scss";

/**
 * Full-screen first-run wizard. Rendered as a sibling of the app chrome (no
 * nav), it walks a fresh install through setup.
 *
 * The rail is filtered by the intent answered on the second screen, and the
 * shell owns skipping: one control, one label, rendered from step.optional, so
 * a step never has to invent its own. A step that cannot be skipped says why in
 * one line instead of offering a button that does nothing useful.
 */
const OnboardingWizardBody: FunctionComponent = () => {
  const { step, next, back, reset } = useWizardStep();
  const { intent, resetIntent } = useOnboardingIntent();
  const navigate = useNavigate();
  const mutation = useSettingsMutation();

  const steps = useMemo(() => stepsForIntent(intent), [intent]);

  const totalSteps = steps.length;
  const activeIndex = Math.min(step, totalSteps - 1);
  const current = steps[activeIndex];
  const StepComponent = current.Component;

  const handleSkip = () => {
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          reset();
          resetIntent();
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
              Step {activeIndex + 1} of {totalSteps} &middot; {current.label}
            </Text>
          </div>
          <Group gap="sm">
            <Button variant="subtle" color="gray" onClick={handleSkip}>
              Skip setup
            </Button>
          </Group>
        </header>

        <div className={styles.rail} aria-hidden>
          {steps.map((s, index) => {
            const done = index < activeIndex;
            const active = index === activeIndex;
            const dotClass = [
              styles.dot,
              done ? styles.dotDone : "",
              active ? styles.dotActive : "",
            ]
              .filter(Boolean)
              .join(" ");
            return (
              <div key={s.key} className={styles.railNode}>
                <span className={dotClass} title={s.label}>
                  {done ? <FontAwesomeIcon icon={faCheck} /> : index + 1}
                </span>
                {index < totalSteps - 1 && (
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
          <StepComponent
            onNext={next}
            onBack={activeIndex > 0 ? back : undefined}
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
    <OnboardingWizardBody />
  </OnboardingIntentProvider>
);

export default OnboardingWizardView;
