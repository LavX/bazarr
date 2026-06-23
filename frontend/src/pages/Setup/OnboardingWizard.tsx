import { FunctionComponent } from "react";
import { useNavigate } from "react-router";
import { Box, Button, Group, Paper, Text } from "@mantine/core";
import { faCheck } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSettingsMutation } from "@/apis/hooks";
import { ONBOARDING_STEPS } from "./steps";
import { useWizardStep } from "./useWizardStep";
import styles from "./OnboardingWizard.module.scss";

/**
 * Full-screen first-run wizard. Rendered as a sibling of the app chrome (no
 * nav), it walks a fresh install through setup. Phase 2 ships the shell plus
 * the Welcome step; later phases push more steps into ONBOARDING_STEPS.
 */
const OnboardingWizardView: FunctionComponent = () => {
  const { step, next, back, reset } = useWizardStep();
  const navigate = useNavigate();
  const mutation = useSettingsMutation();

  const totalSteps = ONBOARDING_STEPS.length;
  const activeIndex = Math.min(step, totalSteps - 1);
  const current = ONBOARDING_STEPS[activeIndex];
  const StepComponent = current.Component;

  const handleSkip = () => {
    mutation.mutate(
      { "settings-general-setup_complete": true },
      {
        onSuccess: () => {
          reset();
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
          {ONBOARDING_STEPS.map((s, index) => {
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
      </div>
    </Box>
  );
};

export default OnboardingWizardView;
