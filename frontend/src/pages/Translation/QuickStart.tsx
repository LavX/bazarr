/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Button,
  PasswordInput,
  SegmentedControl,
} from "@mantine/core";
import { usePointersMutation, useStartEngine } from "@/apis/hooks/translation";
import type {
  TranslationAvailableEngine,
  TranslationEngine,
  TranslationState,
} from "@/types/translation";
import ConsentModal from "./ConsentModal";
import { OfficialMark } from "./EnginePanel";
import InstallConsent from "./InstallConsent";
import { engineName, keyOnlyTypes, orderedEngines } from "./model";
import { useGuardedSave } from "./useGuardedSave";
import styles from "./Translation.module.scss";

/** The key form of the official engine: pick a service, type its key, start. */
const KeyStart: FunctionComponent<{ engine: TranslationEngine }> = ({
  engine,
}) => {
  const types = keyOnlyTypes(engine);
  const [typeId, setTypeId] = useState(types[0]?.typeId ?? "");
  const [key, setKey] = useState("");
  const [missing, setMissing] = useState(false);
  const start = useStartEngine();
  // Set once the back end exists, so a retry only selects the engine.
  const created = useRef(false);
  const picked = types.find((item) => item.typeId === typeId) ?? types[0];
  const form = useGuardedSave((accepted) =>
    start.mutateAsync({
      instanceId: engine.id,
      body: {
        backend_type: picked.typeId,
        // A field that holds several keys takes this one as its first.
        values: {
          [picked.secret.key]: ["key_list", "list"].includes(picked.secret.type)
            ? [key]
            : key,
        },
        accept_destinations: accepted,
      },
      created: created.current,
      onCreated: () => {
        created.current = true;
      },
    }),
  );

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
    if (key.trim() === "") {
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
      {types.length > 1 && (
        <SegmentedControl
          aria-label="Service"
          data={types.map((item) => ({
            value: item.typeId,
            label: item.type.label,
          }))}
          value={picked.typeId}
          disabled={form.busy}
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
        disabled={form.busy}
        error={keyError}
        onChange={(event) => {
          setKey(event.currentTarget.value);
          setMissing(false);
        }}
      />
      {(form.message || otherErrors.length > 0) && !keyError && (
        <Alert color="red" role="alert">
          {otherErrors.length > 0
            ? `${picked.type.label} needs more than a key here: ${otherErrors
                .map(([field, message]) => `${field} ${message}`)
                .join("; ")}. Set it up by hand below.`
            : form.message}
        </Alert>
      )}
      <div>
        <Button type="submit" loading={form.busy}>
          Start translating
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
  /** Leave the panel for the full page, with nothing selected. */
  onByHand: () => void;
}

/**
 * First run: the shortest way to a working set-up. The same choices as the
 * setup wizard's translation step: a key for the official engine, a free
 * engine without a key, or do it by hand.
 */
const QuickStart: FunctionComponent<QuickStartProps> = ({
  state,
  onByHand,
}) => {
  const pointers = usePointersMutation();
  const [installing, setInstalling] =
    useState<TranslationAvailableEngine | null>(null);
  const engines = orderedEngines(state.instances);
  const official = engines.find((item) => item.official) ?? null;
  const available = state.available_engines ?? [];
  const officialOffer = available.find((item) => item.official) ?? null;
  const freeInstalled = engines.filter((item) => !item.official);
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
        {official?.plugin?.active ? (
          keyOnlyTypes(official).length > 0 ? (
            <KeyStart engine={official} />
          ) : (
            <div className={styles.startForm}>
              <div>
                <Button onClick={onByHand}>Set up its back ends</Button>
              </div>
            </div>
          )
        ) : officialOffer ? (
          <div className={styles.startForm}>
            <div>
              <Button onClick={() => setInstalling(officialOffer)}>
                Install {officialOffer.name}
              </Button>
            </div>
          </div>
        ) : (
          <p className={styles.startOptionText}>
            It is not installed, and the plugin catalog does not list it right
            now. Refresh the catalog in the Subtitle Hub, then come back.
          </p>
        )}
      </section>

      {freeInstalled.map((engine) => (
        <section className={styles.startOption} key={engine.id}>
          <div className={styles.startRow}>
            <div>
              <h3 className={styles.startOptionHead}>{engineName(engine)}</h3>
              <p className={styles.startOptionText}>
                Installed and ready. No key needed.
              </p>
            </div>
            <Button
              variant="default"
              loading={pointers.isPending}
              onClick={() => pointers.mutate({ engine: engine.id })}
            >
              Use {engineName(engine)}
            </Button>
          </div>
        </section>
      ))}

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
            <Button variant="default" onClick={() => setInstalling(engine)}>
              Install {engine.name}
            </Button>
          </div>
        </section>
      ))}

      {freeInstalled.length === 0 && freeOffers.length === 0 && (
        <section className={styles.startOption}>
          <h3 className={styles.startOptionHead}>Google Translate</h3>
          <p className={styles.startOptionText}>
            Free, with no key, and plainer translations. It is not in the plugin
            catalog yet. When it is, you can install it from here.
          </p>
        </section>
      )}

      {pointers.error && (
        <Alert color="red" role="alert" mt="md">
          The engine could not be selected. Try again.
        </Alert>
      )}

      <p className={styles.startFoot}>
        Want a local server, a custom address or several back ends?{" "}
        <Anchor component="button" type="button" size="sm" onClick={onByHand}>
          Set it up by hand
        </Anchor>
      </p>

      <InstallConsent engine={installing} onClose={() => setInstalling(null)} />
    </div>
  );
};

export default QuickStart;
