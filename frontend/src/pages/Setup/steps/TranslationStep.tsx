import { FC, useState } from "react";
import { Alert, Button, Group, Skeleton, Stack } from "@mantine/core";
import {
  usePointersMutation,
  useTranslationState,
} from "@/apis/hooks/translation";
import { RestartBanner } from "@/components/hub/RestartBanner";
import { StatusBadge } from "@/components/hub/StatusBadge";
import StepLayout from "@/pages/Setup/StepLayout";
import { useReportStepBusy } from "@/pages/Setup/useStepBusy";
import { OfficialMark } from "@/pages/Translation/EnginePanel";
import {
  canRun,
  engineName,
  engineStatus,
  isFirstRun,
  keyOnlyTypes,
  orderedEngines,
  RESTART_LINES,
} from "@/pages/Translation/model";
import { KeyStart } from "@/pages/Translation/QuickStart";
import {
  useEngineInstall,
  useRestartAware,
} from "@/pages/Translation/useEngineInstall";
import type { TranslationAvailableEngine } from "@/types/translation";
import { NOTHING_WRITTEN, type WizardStepProps } from "./types";
import styles from "@/pages/Translation/Translation.module.scss";

/** The plugin id of the free engine the setup offers beside the AI one. */
const GOOGLE_PLUGIN_ID = "google_translate";

/**
 * An install action. With an offer from the catalog it opens the consent dialog, the one the Plugin
 * Hub shows. Without one it is shown and cannot be pressed, with the reason read out beside it, never
 * hidden.
 */
const InstallAction: FC<{
  name: string;
  offer: TranslationAvailableEngine | null;
  reason: string;
  onInstall: (engine: TranslationAvailableEngine) => void;
  locked: boolean;
}> = ({ name, offer, reason, onInstall, locked }) => {
  if (offer?.awaiting_restart) {
    return (
      <p className={styles.startOptionText}>
        <StatusBadge tone="warning" label="Pending restart" />{" "}
        {RESTART_LINES[offer.awaiting_restart]}
      </p>
    );
  }
  const id = `install-held-${name.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <div className={styles.startForm}>
      <div>
        <Button
          variant="default"
          disabled={offer === null || locked}
          aria-describedby={offer === null ? id : undefined}
          onClick={() => offer && onInstall(offer)}
        >
          Install {name}
        </Button>
      </div>
      {offer === null && (
        <p className={styles.startOptionText} id={id}>
          {reason}
        </p>
      )}
    </div>
  );
};

/**
 * The translation step while the new translator is on: the AI Subtitle Translator with an OpenRouter
 * or a Gemini key, Google Translate as the free choice, or nothing. It is the same choice the
 * Translation page offers on first run, and it saves through the same calls, so a key typed here is
 * stored, switched on and selected exactly as it would be there. The step it replaces is untouched
 * and still runs while the new translator is off.
 *
 * Skipping is the shell's control. Installing an engine opens the same consent dialog as the Plugin
 * Hub: it shows what the install adds and sends its digest. An engine the catalog does not list gets
 * its button disabled, with the reason beside it.
 */
const TranslationStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const loaded = useTranslationState();
  const aware = useRestartAware(loaded);
  const state = { ...loaded, data: aware.state };
  const pointers = usePointersMutation();
  const install = useEngineInstall(state.data);
  const [starting, setStarting] = useState(false);
  // A start that has saved the back end and not finished (the engine would not turn on, or could not be
  // selected) keeps the key form up, with the reason and the way to try again, as the page's first run does.
  const [pinned, setPinned] = useState(false);
  // The shell holds Back, the skip and Set up later until a start has gone through: leaving in the
  // middle would drop the answer, and with it the reason a step failed.
  useReportStepBusy(starting || pointers.isPending || install.busy);

  const data = state.data;
  const engines = data ? orderedEngines(data.instances) : [];
  const official = engines.find((item) => item.official) ?? null;
  const officialOffer =
    data?.available_engines?.find((item) => item.official) ?? null;
  const googleInstalled =
    engines.find((item) => item.plugin_id === GOOGLE_PLUGIN_ID) ?? null;
  const googleOffer =
    data?.available_engines?.find(
      (item) => item.plugin_id === GOOGLE_PLUGIN_ID,
    ) ?? null;
  // Any other engine that is installed and ready is offered as it is, as on the page. Lingarr is a
  // legacy engine and stays one: it is set up in its own place, not offered here.
  const others = engines.filter(
    (item) =>
      !item.official && !item.legacy && item.plugin_id !== GOOGLE_PLUGIN_ID,
  );
  const busy = starting || pointers.isPending || install.busy;
  // Translation is already set up (an engine is selected, or a back end is saved): the step says so and
  // offers nothing that would add a second back end, as typing the key again after a Back would.
  const saved = data !== undefined && !isFirstRun(data);
  const setUp = saved && !pinned && !starting;
  const selected = data?.instances.find((item) => item.id === data.engine);
  const selectedStatus = selected ? engineStatus(selected) : null;

  const use = (id: number) =>
    pointers.mutate({ engine: id }, { onSuccess: () => onNext() });

  const readyEngine = (engine: (typeof engines)[number]) => {
    const status = engineStatus(engine);
    return (
      <>
        <h3 className={styles.startOptionHead}>
          {engineName(engine)}
          <StatusBadge tone={status.tone} label={status.label} />
        </h3>
        <p className={styles.startOptionText}>
          {status.tone === "success"
            ? engine.description || "Installed. It works as it is."
            : status.next}
        </p>
        {status.tone === "success" && (
          <div className={styles.startForm}>
            <div>
              <Button
                variant="default"
                loading={pointers.isPending}
                disabled={starting}
                onClick={() => use(engine.id)}
              >
                Use {engineName(engine)}
              </Button>
            </div>
          </div>
        )}
      </>
    );
  };

  return (
    <StepLayout
      title="Subtitle translation"
      layout="stacked"
      aside={<RestartBanner count={aware.waiting} />}
      description="When no subtitle exists in your language, Bazarr+ can translate one from another language. Pick what does the translating. You can change all of it later on the Translation page."
      actions={
        <Stack gap="sm">
          {pointers.error && (
            <Alert color="red" role="alert">
              The engine could not be selected. Try again, or set translation up
              later on the Translation page.
            </Alert>
          )}
          <Group justify="space-between">
            <Group gap="sm">
              {onBack && (
                <Button variant="default" onClick={onBack} disabled={busy}>
                  Back
                </Button>
              )}
            </Group>
            {saved ? (
              // Something was saved, by this visit or an earlier one: that is an answer, though
              // Continue writes nothing.
              <Button onClick={() => onNext()} disabled={busy}>
                Continue
              </Button>
            ) : (
              <Button onClick={() => onNext(NOTHING_WRITTEN)} disabled={busy}>
                Continue without translation
              </Button>
            )}
          </Group>
        </Stack>
      }
    >
      {state.isLoading && !data ? (
        <div aria-busy="true" aria-label="Loading translation">
          <Skeleton height={120} radius="md" mb="sm" />
          <Skeleton height={84} radius="md" />
        </div>
      ) : !data ? (
        <Alert color="red" role="alert" title="Translation could not be loaded">
          Bazarr+ did not answer. You can continue and set translation up later
          on the Translation page.
          <div>
            <Button
              mt="sm"
              size="xs"
              variant="default"
              loading={state.isFetching}
              onClick={() => void state.refetch()}
            >
              Try again
            </Button>
          </div>
        </Alert>
      ) : setUp ? (
        <section className={styles.startOption} data-primary="true">
          <h3 className={styles.startOptionHead}>
            Translation is set up
            {selectedStatus && (
              <StatusBadge
                tone={selectedStatus.tone}
                label={selectedStatus.label}
              />
            )}
          </h3>
          <p className={styles.startOptionText}>
            {selected && selectedStatus?.tone === "success"
              ? `${engineName(selected)} is selected and ready.`
              : selected
                ? `${engineName(selected)} is selected. ${selectedStatus?.next}`
                : "A back end is saved, but no engine is selected, so nothing translates yet. Finish it on the Translation page."}{" "}
            Change any of it later on the Translation page.
          </p>
        </section>
      ) : (
        <div>
          <section className={styles.startOption} data-primary="true">
            <h3 className={styles.startOptionHead}>
              {official
                ? engineName(official)
                : (officialOffer?.name ?? "AI Subtitle Translator")}
              <OfficialMark />
            </h3>
            <p className={styles.startOptionText}>
              Recommended. It translates with an AI service through your own
              key, and you pay that service for what you translate. OpenRouter
              and Gemini both work with a key alone.
            </p>
            {official && canRun(official) ? (
              keyOnlyTypes(official).length > 0 ? (
                <KeyStart
                  engine={official}
                  onPin={setPinned}
                  onBusy={setStarting}
                  locked={pointers.isPending}
                  // Setup has no set-up form to open: the Translation page has it, and the key
                  // is saved, so leaving is an answer.
                  onOpenSetup={() => onNext()}
                  openLabel="Continue and finish on the Translation page"
                  onStarted={() => onNext()}
                />
              ) : (
                <p className={styles.startOptionText}>
                  Add its back ends on the Translation page once setup is done.
                </p>
              )
            ) : official ? (
              <p className={styles.startOptionText}>
                <StatusBadge
                  tone={engineStatus(official).tone}
                  label={engineStatus(official).label}
                />{" "}
                {engineStatus(official).next}
              </p>
            ) : (
              <InstallAction
                name={officialOffer?.name ?? "AI Subtitle Translator"}
                offer={officialOffer}
                reason="It is not installed, and the plugin catalog does not list it right now. Refresh the catalog in the Plugin Hub, then come back."
                onInstall={install.begin}
                locked={busy}
              />
            )}
          </section>

          <section className={styles.startOption}>
            {googleInstalled &&
            (googleInstalled.plugin !== null ||
              googleInstalled.removing ||
              !googleOffer) ? (
              readyEngine(googleInstalled)
            ) : (
              <>
                <h3 className={styles.startOptionHead}>
                  {googleOffer?.name ?? "Google Translate"}
                </h3>
                <p className={styles.startOptionText}>
                  {googleOffer?.description ||
                    "Free, with no key, and plainer translations than the AI engine."}
                </p>
                <InstallAction
                  name={googleOffer?.name ?? "Google Translate"}
                  offer={googleOffer}
                  reason="It is not in the plugin catalog yet. When it is, you can install it from here."
                  onInstall={install.begin}
                  locked={busy}
                />
              </>
            )}
          </section>

          {others.map((engine) => (
            <section className={styles.startOption} key={engine.id}>
              {readyEngine(engine)}
            </section>
          ))}
        </div>
      )}
      {install.dialog}
    </StepLayout>
  );
};

export default TranslationStep;
