/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  PasswordInput,
  SegmentedControl,
} from "@mantine/core";
import {
  EngineStillOffError,
  usePointersMutation,
  useStartEngine,
} from "@/apis/hooks/translation";
import { StatusBadge } from "@/components/hub/StatusBadge";
import type {
  TranslationAvailableEngine,
  TranslationEngine,
  TranslationState,
} from "@/types/translation";
import ConsentModal from "./ConsentModal";
import { LEGACY_NOTE, LegacyMark, OfficialMark } from "./EnginePanel";
import InstallConsent from "./InstallConsent";
import {
  canRun,
  engineName,
  engineStatus,
  keyOnlyTypes,
  orderedEngines,
} from "./model";
import { useGuardedSave } from "./useGuardedSave";
import styles from "./Translation.module.scss";

/** The key form of the official engine: pick a service, type its key, start. */
const KeyStart: FunctionComponent<{
  engine: TranslationEngine;
  onPin: (pinned: boolean) => void;
  /** Told while a start is on its way, so the rest of the panel can wait for it. */
  onBusy: (busy: boolean) => void;
  /** Another choice on the panel is on its way: this form waits for it. */
  locked: boolean;
  /** Opens the engine's set-up in the full page, where its switch is. */
  onOpenSetup: () => void;
}> = ({ engine, onPin, onBusy, locked, onOpenSetup }) => {
  const types = keyOnlyTypes(engine);
  const [typeId, setTypeId] = useState(types[0]?.typeId ?? "");
  const [key, setKey] = useState("");
  const [missing, setMissing] = useState(false);
  const start = useStartEngine();
  // Set once the back end exists, so a retry only selects the engine.
  const created = useRef(false);
  const [keySaved, setKeySaved] = useState(false);
  // The engine was off and the server would not turn it on: "settings" when it named settings that
  // do not fit, "refused" for any other answer. Nothing was selected, so the start did not go through.
  const [stillOff, setStillOff] = useState<"settings" | "refused" | null>(null);
  const picked = types.find((item) => item.typeId === typeId) ?? types[0];
  const form = useGuardedSave(async (accepted) => {
    // The panel stays up from here until the start has gone through: a refresh in between would
    // otherwise replace it, and the reason a step failed would go with it.
    onPin(true);
    onBusy(true);
    setStillOff(null);
    try {
      await start.mutateAsync({
        instanceId: engine.id,
        body: {
          backend_type: picked.typeId,
          // A field that holds several keys takes this one as its first.
          values: {
            [picked.secret.key]: ["key_list", "list"].includes(
              picked.secret.type,
            )
              ? [key]
              : key,
          },
          accept_destinations: accepted,
        },
        created: created.current,
        onCreated: () => {
          created.current = true;
          setKeySaved(true);
          // The key is stored. It has no reason to stay in the page.
          setKey("");
        },
      });
    } catch (error) {
      // Nothing was saved: the panel is where it would be anyway.
      if (!created.current) {
        onPin(false);
      }
      if (error instanceof EngineStillOffError) {
        setStillOff(
          Object.keys(error.refusal?.fieldErrors ?? {}).length > 0
            ? "settings"
            : "refused",
        );
      }
      throw error;
    } finally {
      onBusy(false);
    }
    onPin(false);
  });

  if (!picked) {
    return null;
  }
  const model = picked.type.fields.find((field) => field.key === "model");
  const keyLabel = `${picked.type.label} key`;
  const keyError = missing
    ? "Type the key first."
    : form.fieldErrors[picked.secret.key];
  // A refusal about anything other than the key cannot be fixed in this form.
  const otherErrors = Object.entries(form.fieldErrors).filter(
    ([field]) => field !== picked.secret.key,
  );
  const submit = () => {
    if (!keySaved && key.trim() === "") {
      setMissing(true);
      return;
    }
    void form.save();
  };

  return (
    <form
      className={styles.startForm}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {types.length > 1 && !keySaved && (
        <SegmentedControl
          aria-label="Service"
          data={types.map((item) => ({
            value: item.typeId,
            label: item.type.label,
          }))}
          value={picked.typeId}
          disabled={form.busy || locked}
          onChange={(next) => {
            setTypeId(next);
            setKey("");
            setMissing(false);
            form.dismiss();
          }}
        />
      )}
      <PasswordInput
        label={keyLabel}
        autoComplete="off"
        description={
          <>
            {typeof model?.default === "string" && model.default
              ? `Starts with the ${model.default} model. You can change it later. `
              : ""}
            {picked.secret.help_url && (
              <Anchor
                href={picked.secret.help_url}
                target="_blank"
                rel="noopener noreferrer"
                size="xs"
              >
                Get a key
              </Anchor>
            )}
          </>
        }
        value={key}
        placeholder={keySaved ? "Saved" : undefined}
        disabled={form.busy || keySaved || locked}
        error={keyError}
        onChange={(event) => {
          setKey(event.currentTarget.value);
          setMissing(false);
        }}
      />
      {stillOff && (
        <Alert color="red" role="alert">
          {stillOff === "settings"
            ? `Your key is saved, but ${engineName(engine)} is turned off and cannot be turned on until its settings are checked. Nothing translates yet. Open its set-up, check and save its settings, then turn it on there.`
            : `Your key is saved, but ${engineName(engine)} is turned off and could not be turned on. Nothing translates yet. Try again, or open its set-up and turn it on there.`}
          <div>
            <Button
              mt="xs"
              size="xs"
              variant="default"
              disabled={form.busy || locked}
              onClick={onOpenSetup}
            >
              Open its set-up
            </Button>
          </div>
        </Alert>
      )}
      {(form.message || otherErrors.length > 0) && !keyError && !stillOff && (
        <Alert color="red" role="alert">
          {keySaved
            ? "Your key is saved, but the engine could not be switched on. Try again; nothing needs typing again."
            : otherErrors.length > 0
              ? `${picked.type.label} needs more than a key here: ${otherErrors
                  .map(([field, message]) => `${field} ${message}`)
                  .join("; ")}. Set it up by hand below.`
              : form.message}
        </Alert>
      )}
      <div>
        <Button type="submit" loading={form.busy} disabled={locked}>
          {keySaved ? "Switch the engine on" : "Start translating"}
        </Button>
      </div>
      <ConsentModal
        destinations={form.asking}
        busy={form.busy}
        onAccept={() => void form.accept()}
        onCancel={form.decline}
      />
    </form>
  );
};

export interface QuickStartProps {
  state: TranslationState;
  /** Leave the panel for the full page, with nothing selected, on this engine's set-up when one is named. */
  onByHand: (engineId?: number) => void;
  /** Keep the panel up (true) until a start that is under way has gone through (false). */
  onPin: (pinned: boolean) => void;
}

/**
 * First run: the shortest way to a working set-up. The same choices as the
 * setup wizard's translation step: a key for the official engine, a free
 * engine without a key, or do it by hand.
 */
const QuickStart: FunctionComponent<QuickStartProps> = ({
  state,
  onByHand,
  onPin,
}) => {
  const pointers = usePointersMutation();
  // One choice at a time on the whole panel: while a start or a pick is on its way, nothing else can
  // be chosen, so an answer that comes back late cannot replace a newer choice.
  const [starting, setStarting] = useState(false);
  const busy = starting || pointers.isPending;
  const [installing, setInstalling] =
    useState<TranslationAvailableEngine | null>(null);
  const engines = orderedEngines(state.instances);
  const official = engines.find((item) => item.official) ?? null;
  // A server that does not list what can be installed has said nothing about the catalog, which is
  // not the same as a catalog that offers nothing.
  const catalogKnown = state.available_engines !== undefined;
  const available = state.available_engines ?? [];
  const officialOffer = available.find((item) => item.official) ?? null;
  const otherInstalled = engines.filter((item) => !item.official);
  const freeOffers = available.filter((item) => !item.official && item.free);

  return (
    <div className={styles.start}>
      <h2 className={styles.startTitle}>
        Translate the subtitles you cannot find
      </h2>
      <p className={styles.startLead}>
        When no subtitle exists in your language, Bazarr+ can translate one from
        another language. Pick what does the translating.
      </p>

      <section className={styles.startOption} data-primary="true">
        <h3 className={styles.startOptionHead}>
          {official
            ? engineName(official)
            : (officialOffer?.name ?? "AI Subtitle Translator")}
          <OfficialMark />
        </h3>
        <p className={styles.startOptionText}>
          Recommended. It translates with an AI service through your own key,
          and you pay that service for what you translate.
        </p>
        {official && canRun(official) ? (
          keyOnlyTypes(official).length > 0 ? (
            <KeyStart
              engine={official}
              onPin={onPin}
              onBusy={setStarting}
              locked={pointers.isPending}
              onOpenSetup={() => {
                // The start is over as far as this panel goes: the full page takes it from here.
                onPin(false);
                onByHand(official.id);
              }}
            />
          ) : (
            <div className={styles.startForm}>
              <div>
                <Button disabled={busy} onClick={() => onByHand(official.id)}>
                  Set up its back ends
                </Button>
              </div>
            </div>
          )
        ) : officialOffer ? (
          <div className={styles.startForm}>
            <div>
              <Button
                disabled={busy}
                onClick={() => setInstalling(officialOffer)}
              >
                Install {officialOffer.name}
              </Button>
            </div>
          </div>
        ) : official ? (
          // Installed and not able to run: what it reports, not "not installed".
          <p className={styles.startOptionText}>
            <StatusBadge
              tone={engineStatus(official).tone}
              label={engineStatus(official).label}
            />{" "}
            {engineStatus(official).next}
          </p>
        ) : (
          <p className={styles.startOptionText}>
            {catalogKnown
              ? "It is not installed, and the plugin catalog does not list it right now. Refresh the catalog in the Subtitle Hub, then come back."
              : "It is not installed. Install it in the Subtitle Hub, then come back."}
          </p>
        )}
      </section>

      {otherInstalled.map((engine) => {
        // What the engine itself reports: it is offered for use only when it can take a job.
        const status = engineStatus(engine);
        const ready = status.tone === "success";
        return (
          <section className={styles.startOption} key={engine.id}>
            <div className={styles.startRow}>
              <div>
                <h3 className={styles.startOptionHead}>
                  {engineName(engine)}
                  {engine.legacy && <LegacyMark />}
                  <StatusBadge tone={status.tone} label={status.label} />
                </h3>
                <p className={styles.startOptionText}>
                  {ready
                    ? engine.description || "Installed. It works as it is."
                    : status.next}
                  {engine.legacy && ` ${LEGACY_NOTE}`}
                </p>
              </div>
              {ready ? (
                <Button
                  variant="default"
                  loading={pointers.isPending}
                  disabled={starting}
                  onClick={() =>
                    pointers.mutate(
                      { engine: engine.id },
                      // The panel may be held up by a start that stopped half way: picking another
                      // engine settles it, and the page moves on.
                      { onSuccess: () => onPin(false) },
                    )
                  }
                >
                  Use {engineName(engine)}
                </Button>
              ) : (
                canRun(engine) && (
                  <Button
                    variant="default"
                    disabled={busy}
                    onClick={() => onByHand(engine.id)}
                  >
                    Set up {engineName(engine)}
                  </Button>
                )
              )}
            </div>
          </section>
        );
      })}

      {freeOffers.map((engine) => (
        <section className={styles.startOption} key={engine.plugin_id}>
          <div className={styles.startRow}>
            <div>
              <h3 className={styles.startOptionHead}>{engine.name}</h3>
              <p className={styles.startOptionText}>
                {engine.description ||
                  "Free, with no key. Plainer translations than the AI engine."}
              </p>
            </div>
            <Button
              variant="default"
              disabled={busy}
              onClick={() => setInstalling(engine)}
            >
              Install {engine.name}
            </Button>
          </div>
        </section>
      ))}

      {otherInstalled.length === 0 &&
        freeOffers.length === 0 &&
        (catalogKnown ? (
          <section className={styles.startOption}>
            <h3 className={styles.startOptionHead}>Google Translate</h3>
            <p className={styles.startOptionText}>
              Free, with no key, and plainer translations. It is not in the
              plugin catalog yet. When it is, you can install it from here.
            </p>
          </section>
        ) : (
          <section className={styles.startOption}>
            <h3 className={styles.startOptionHead}>Other engines</h3>
            <p className={styles.startOptionText}>
              An engine you install in the Subtitle Hub shows up here, ready to
              pick.
            </p>
          </section>
        ))}

      {pointers.error && (
        <Alert color="red" role="alert" mt="md">
          The engine could not be selected. Try again.
        </Alert>
      )}

      <p className={styles.startFoot}>
        Want a local server, a custom address or several back ends?{" "}
        <Anchor
          component="button"
          type="button"
          size="sm"
          disabled={busy}
          className={styles.startLink}
          onClick={() => onByHand()}
        >
          Set it up by hand
        </Anchor>
      </p>

      <InstallConsent
        key={installing?.plugin_id ?? "none"}
        engine={installing}
        onClose={() => setInstalling(null)}
      />
    </div>
  );
};

export default QuickStart;
