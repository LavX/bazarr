import { FC, useRef, useState } from "react";
import {
  Alert,
  Button,
  Group,
  NumberInput,
  PasswordInput,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import {
  getArrInstanceDeleteConflict,
  getArrInstanceErrorMessage,
  useArrInstances,
  useCreateArrInstance,
  useDeleteArrInstance,
  useSettingsMutation,
  useSystemSettings,
  useTestArrInstanceConnection,
  useUpdateArrInstance,
} from "@/apis/hooks";
import type {
  ArrInstanceCreate,
  ArrInstanceTest,
} from "@/apis/raw/arrInstances";
import {
  ConnectionTest,
  connectionTestKey,
  readConnectionTest,
  recordConnectionTest,
} from "@/pages/Setup/connectionTests";
import StepLayout from "@/pages/Setup/StepLayout";
import { useStepDraft } from "@/pages/Setup/useStepDrafts";
import type { WizardStepProps } from "./types";

export interface ArrStepProps extends WizardStepProps {
  kind: "sonarr" | "radarr" | "sportarr";
}

const KIND_META: Record<
  ArrStepProps["kind"],
  { label: string; media: string; port: number }
> = {
  sonarr: { label: "Sonarr", media: "TV shows", port: 8989 },
  radarr: { label: "Radarr", media: "movies", port: 7878 },
  sportarr: { label: "Sportarr", media: "sports events", port: 1867 },
};

// The saved panel says what Finish will say about the row: saving proves the
// values were written, and only a passing Test proves they connect.
const SAVED_TITLE: Record<ConnectionTest, string> = {
  passed: "Already connected",
  failed: "Saved, but the connection test failed",
  untested: "Saved, connection not tested",
};

function normalizeBaseUrl(value: string) {
  const trimmed = value.trim().replace(/^\/+/, "").replace(/\/+$/, "");
  return trimmed ? `/${trimmed}` : "";
}

interface SplitAddress {
  ip: string;
  scheme?: "http" | "https";
  port?: number;
  baseUrl?: string;
}

/**
 * A URL put into Address, taken apart into the fields it belongs in. Address
 * is stored verbatim as the host, so a newcomer running Sonarr in Docker who
 * typed http://sonarr:8989 saved an instance that could never connect.
 *
 * Read from the text rather than through URL: URL drops a port that is the
 * scheme's default, so https://sonarr:443 came back with no port at all and
 * the form kept 8989 while turning SSL on. A bare IPv6 address has several
 * colons and no port, and is left alone.
 */
function splitAddress(value: string): SplitAddress | null {
  const trimmed = value.trim();
  const schemeMatch = /^(https?):\/\//i.exec(trimmed);
  const scheme = schemeMatch
    ? (schemeMatch[1].toLowerCase() as "http" | "https")
    : undefined;
  const rest = schemeMatch ? trimmed.slice(schemeMatch[0].length) : trimmed;
  const cut = rest.search(/[/?#]/);
  const authority = cut < 0 ? rest : rest.slice(0, cut);
  const path =
    cut < 0
      ? ""
      : rest
          .slice(cut)
          .replace(/[?#].*$/, "")
          .replace(/\/+$/, "");
  const hostPort = authority.slice(authority.lastIndexOf("@") + 1);
  const portMatch = /^(\[[^\]]*\]|[^:]*):(\d{1,5})$/.exec(hostPort);
  const portNumber = portMatch ? Number(portMatch[2]) : NaN;
  const port = portNumber >= 1 && portNumber <= 65535 ? portNumber : undefined;
  if (scheme === undefined && port === undefined && path === "") {
    return null;
  }
  return {
    ip: port !== undefined && portMatch ? portMatch[1] : hostPort,
    scheme,
    port,
    baseUrl: path ? path : undefined,
  };
}

function describeSplit(split: SplitAddress, label: string): string {
  const parts: string[] = [];
  if (split.scheme !== undefined) {
    parts.push(`Bazarr+ took the ${split.scheme}:// off the address.`);
  }
  if (split.port !== undefined) {
    parts.push("The port went to Port.");
  }
  if (split.baseUrl !== undefined) {
    parts.push("The path went to Base URL.");
  }
  if (split.scheme === "https") {
    parts.push("Use SSL is on, because the address was https.");
  }
  parts.push(`Check that this is where ${label} is reachable.`);
  return parts.join(" ");
}

interface FieldErrors {
  ip?: string;
  port?: string;
  apiKey?: string;
}

/**
 * Onboarding connection step for a single arr kind. Mirrors the InstanceFormModal
 * field set with bespoke inputs (the modal is not reused here). Tests the typed
 * connection, then on Continue creates the instance, flips use_<kind> on, and
 * advances. Idempotent: if an instance of this kind already exists, it shows the
 * saved row and Continue advances without creating a duplicate, turning the row
 * and use_<kind> back on first when either was switched off since.
 *
 * Every arr kind is optional, including Sonarr: Bazarr+ runs with no instance
 * at all. Skipping is the shell's job, so there is no skip control here.
 *
 * A step nobody filled in writes nothing. It used to post the untouched form,
 * and the backend fills the blanks in (empty key, 127.0.0.1), so pressing
 * Continue three times on the library path left three enabled instances
 * pointing at nothing, each one scheduled for sync, each one a permanent
 * health warning. So: an untouched step advances without creating, and a
 * touched one has to name an address and a key before anything is written.
 */
const ArrStep: FC<ArrStepProps> = ({ kind, onNext, onBack, stepKey }) => {
  const meta = KIND_META[kind];

  const { data: instances } = useArrInstances();
  // Every failure here is shown inline, beside the control that caused it, so
  // the hooks keep their toasts to themselves.
  const create = useCreateArrInstance({ inlineErrors: true });
  const remove = useDeleteArrInstance({ inlineErrors: true });
  const update = useUpdateArrInstance({ inlineErrors: true });
  const settings = useSettingsMutation(false, { inlineErrors: true });
  const { data: systemSettings } = useSystemSettings();

  // An enabled row first: with one of the kind on and another off, the one
  // that is on is the connection, and Continue has nothing to turn back on.
  const ofKind = (instances ?? []).filter((i) => i.kind === kind);
  const existing = ofKind.find((i) => i.enabled) ?? ofKind[0] ?? null;
  // A rerun of setup can find the saved row, or the kind's master switch,
  // turned off since. Either one alone stops the sync. Unknown until the
  // settings answer, and Continue writes the switch rather than assume it.
  const kindOn = systemSettings?.general?.[`use_${kind}`] === true;
  const kindKnownOff = systemSettings?.general !== undefined && !kindOn;

  // Parked on the wizard rather than in this component: the shell remounts a
  // step whenever the cursor moves, so going back one screen to re-read a
  // question used to empty a filled-in form with no warning that it would.
  // The API key is deliberately not in here. It is a credential, and a
  // credential that outlives the screen it was typed on is one more place it
  // can be read from; it is also the one field a reader expects to retype.
  const [draft, patchDraft] = useStepDraft(stepKey, {
    name: `Main ${meta.label}`,
    ip: "",
    port: meta.port as number | string,
    baseUrl: "",
    ssl: false,
  });
  const { name, ip, port, baseUrl, ssl } = draft;
  const setName = (value: string) => patchDraft({ name: value });
  const setIp = (value: string) => patchDraft({ ip: value });
  const setPort = (value: number | string) => patchDraft({ port: value });
  const setBaseUrl = (value: string) => patchDraft({ baseUrl: value });
  const setSsl = (value: boolean) => patchDraft({ ssl: value });
  const [apiKey, setApiKey] = useState("");
  const [splitNote, setSplitNote] = useState<string | null>(null);
  const pasted = useRef(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  // The row this step wrote, remembered here rather than waited for. The create
  // invalidates the instances query, so `existing` normally catches up on its
  // own, but a refetch that is slow or that fails leaves it null with the row
  // already in the database. Continue would then post a second identical
  // instance instead of retrying the part that failed.
  const [createdId, setCreatedId] = useState<number | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const trimmedIp = ip.trim();
  const trimmedKey = apiKey.trim();
  const normalizedBaseUrl = normalizeBaseUrl(baseUrl);
  // A cleared NumberInput hands back "", and Number("") is 0, which the backend
  // rejects with a message this step never showed. NaN and 0 both fail here.
  const portNumber = typeof port === "number" ? port : Number(port);
  const portValid =
    Number.isInteger(portNumber) && portNumber >= 1 && portNumber <= 65535;

  // The same rule the media-server step uses: what the reader typed, not what
  // they clicked. Nothing typed means nothing to save.
  const touched =
    trimmedIp.length > 0 || trimmedKey.length > 0 || baseUrl.trim().length > 0;
  const testable = trimmedIp.length > 0 && trimmedKey.length > 0 && portValid;

  const test = useTestArrInstanceConnection(
    {
      ip: trimmedIp,
      port: portNumber,
      baseUrl: normalizedBaseUrl,
      ssl,
      apiKey: trimmedKey,
    },
    // A Test that cannot reach Bazarr+ says so beside the button.
    { inlineErrors: true },
  );

  const clearError = (field: keyof FieldErrors) => {
    setErrors((current) => {
      if (current[field] === undefined) {
        return current;
      }
      const next = { ...current };
      delete next[field];
      return next;
    });
  };
  // Takes a URL in Address apart into Address, Port, Base URL and Use SSL, and
  // says so. Answers whether there was anything to take apart.
  const applySplit = (value: string): boolean => {
    const split = splitAddress(value);
    if (split === null) {
      return false;
    }
    patchDraft({
      ip: split.ip,
      ...(split.port !== undefined ? { port: split.port } : {}),
      ...(split.baseUrl !== undefined ? { baseUrl: split.baseUrl } : {}),
      ...(split.scheme !== undefined ? { ssl: split.scheme === "https" } : {}),
    });
    if (split.port !== undefined) {
      clearError("port");
    }
    setSplitNote(describeSplit(split, meta.label));
    return true;
  };

  const buildOverrides = () => ({
    ip: trimmedIp,
    port: portNumber,
    base_url: normalizedBaseUrl,
    ssl,
    verify_ssl: true,
    http_timeout: 60,
  });

  const handleTest = () => {
    const body: ArrInstanceTest = { kind, ...buildOverrides() };
    if (trimmedKey) {
      body.api_key = trimmedKey;
    }
    test.mutate(body);
  };

  const validate = (): FieldErrors => {
    const found: FieldErrors = {};
    if (!trimmedIp) {
      found.ip = `Enter the address where ${meta.label} is reachable`;
    }
    if (!portValid) {
      found.port = "Port must be between 1 and 65535";
    }
    if (!trimmedKey) {
      found.apiKey = `Enter the ${meta.label} API key`;
    }
    return found;
  };

  // The row is what Bazarr+ syncs; the master switch is what lets it. Firing
  // both on the same tick left instances that never synced when the switch
  // failed, with nothing on screen to say so.
  const activate = () => {
    settings.mutate(
      { [`settings-general-use_${kind}`]: true },
      {
        onSuccess: () => onNext(),
        onError: () =>
          setSaveError(
            `${meta.label} was saved, but Bazarr+ could not turn it on. Open Settings, Connections after setup and enable ${meta.label} there.`,
          ),
      },
    );
  };

  const handleContinue = () => {
    if (existing) {
      setSaveError(null);
      if (!existing.enabled) {
        update.mutate(
          { id: existing.id, body: { enabled: true } },
          {
            onSuccess: () => activate(),
            onError: (error) =>
              setSaveError(
                getArrInstanceErrorMessage(
                  error,
                  `Bazarr+ could not turn ${existing.name} back on. Open Settings, Connections after setup and enable it there.`,
                ),
              ),
          },
        );
        return;
      }
      if (!kindOn) {
        activate();
        return;
      }
      onNext();
      return;
    }
    // The instance is already written, whatever the list says. Pressing
    // Continue again retries the switch that failed; it never creates a
    // second row.
    if (createdId !== null) {
      setSaveError(null);
      activate();
      return;
    }
    if (!touched) {
      onNext();
      return;
    }
    const found = validate();
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    setSaveError(null);
    // Saving does not wait for a Test, so Finish is told what the Test said
    // about these exact values. The hook drops its verdict whenever a
    // connection field changes, so a result here is about what is on screen.
    const tested = test.data
      ? test.data.ok
        ? "passed"
        : "failed"
      : "untested";
    const body: ArrInstanceCreate = {
      kind,
      name: name.trim(),
      ...buildOverrides(),
      api_key: trimmedKey,
      enabled: true,
      // No is_default: the backend makes the first enabled instance of a kind
      // the default on its own. Claiming it here made every wizard row the
      // default, including the ones nobody confirmed.
    };
    create.mutate(body, {
      onSuccess: (created) => {
        setCreatedId(created.id);
        recordConnectionTest(connectionTestKey("arr", created.id), tested);
        activate();
      },
      onError: (error) =>
        setSaveError(
          getArrInstanceErrorMessage(
            error,
            `Bazarr+ could not save the ${meta.label} connection.`,
          ),
        ),
    });
  };

  const handleRemoveExisting = () => {
    if (!existing) {
      return;
    }
    setRemoveError(null);
    remove.mutate(
      { id: existing.id },
      {
        onSuccess: () => {
          setConfirmRemove(false);
          // The row this message was about is gone, so the message goes with it,
          // and so does the memory of having written it.
          setSaveError(null);
          setCreatedId(null);
        },
        onError: (error) =>
          setRemoveError(
            // Deleting it together with its library is offered in Settings,
            // Connections only, behind its own confirmation.
            getArrInstanceDeleteConflict(error)?.can_remove_library
              ? `${existing.name} has already synced its library, so setup cannot remove it on its own. After setup, you can delete it together with that library in Settings, Connections.`
              : getArrInstanceErrorMessage(
                  error,
                  `Bazarr+ could not remove ${existing.name}. You can edit it in Settings, Connections after setup.`,
                ),
          ),
      },
    );
  };

  const testResult = test.data;

  if (existing) {
    const address = `${existing.ssl ? "https" : "http"}://${existing.ip}:${
      existing.port
    }${existing.base_url && existing.base_url !== "/" ? existing.base_url : ""}`;
    const savedTest = readConnectionTest(connectionTestKey("arr", existing.id));
    return (
      <StepLayout
        title={meta.label}
        description={`Connect ${meta.label} for your ${meta.media}.`}
        actions={
          <Group justify="space-between">
            <Group gap="sm">
              {onBack && (
                <Button variant="default" onClick={onBack}>
                  Back
                </Button>
              )}
            </Group>
            <Button
              onClick={handleContinue}
              loading={update.isPending || settings.isPending}
            >
              Continue
            </Button>
          </Group>
        }
      >
        <Alert
          color={savedTest === "passed" ? "green" : "yellow"}
          title={SAVED_TITLE[savedTest]}
        >
          <Stack gap="sm" align="flex-start">
            <Text size="sm">
              {existing.name} at {address}
            </Text>
            {/* Not while a write is in flight: a row created a moment ago
                comes back before its switch lands, and was never off. */}
            {(!existing.enabled || kindKnownOff) &&
              !update.isPending &&
              !settings.isPending && (
                <Text size="sm">
                  It is switched off, so Bazarr+ does not sync it. Continue
                  turns it back on.
                </Text>
              )}
            {/* Wrong address, wrong key, wrong instance: without this the only
                way back out of a typo was to finish the wizard and find
                Settings, Connections. */}
            {confirmRemove ? (
              <Group gap="sm">
                <Button
                  size="xs"
                  color="red"
                  loading={remove.isPending}
                  onClick={handleRemoveExisting}
                >
                  Remove {existing.name}
                </Button>
                <Button
                  size="xs"
                  variant="default"
                  onClick={() => setConfirmRemove(false)}
                >
                  Keep it
                </Button>
              </Group>
            ) : (
              <Button
                size="xs"
                variant="subtle"
                color="red"
                onClick={() => setConfirmRemove(true)}
              >
                Remove and enter it again
              </Button>
            )}
          </Stack>
        </Alert>
        {/* The create landing is what makes this branch render: the instances
            query is invalidated by it, so a switch write that failed a moment
            later reports itself here or nowhere. Without this the reader was
            shown a green "Already connected" and walked on with the kind still
            switched off. */}
        {saveError && (
          <Alert color="red" title={`Could not connect ${meta.label}`}>
            {saveError}
          </Alert>
        )}
        {removeError && (
          <Alert color="red" title="Could not remove the instance">
            {removeError}
          </Alert>
        )}
      </StepLayout>
    );
  }

  const verdict = (
    <Stack gap="sm">
      {testResult &&
        (testResult.ok ? (
          <Alert
            color="green"
            title={`Connected to ${testResult.app_name ?? meta.label}`}
          >
            {testResult.version
              ? `Version ${testResult.version}`
              : "The instance responded successfully."}
          </Alert>
        ) : (
          <Alert color="red" title="Connection failed">
            {testResult.message ??
              testResult.error ??
              "The instance did not respond."}
          </Alert>
        ))}
      {test.isError && (
        <Alert color="red" title="Test failed">
          Could not reach the Bazarr API to run the connection test.
        </Alert>
      )}
    </Stack>
  );

  return (
    <StepLayout
      title={meta.label}
      description={`Connect ${meta.label} so Bazarr can find your ${meta.media}.`}
      aside={verdict}
      actions={
        <Stack gap={6}>
          {/* Beside Continue, which is what produced it. In the side column it
              sat a whole form away from the button at every width. */}
          {saveError && (
            <Alert color="red" title={`Could not connect ${meta.label}`}>
              {saveError}
            </Alert>
          )}
          {!testable && (
            <Text size="sm" c="dimmed" ta="right">
              Enter an address and an API key to test this connection.
            </Text>
          )}
          <Group justify="space-between">
            <Group gap="sm">
              {onBack && (
                <Button variant="default" onClick={onBack}>
                  Back
                </Button>
              )}
            </Group>
            <Group gap="sm">
              {/* Beside Continue, because testing is the last thing a reader
                  does before pressing it. An ungated Test probed 127.0.0.1 on
                  the default port with an empty key and reported a verdict
                  about a server the reader never named. */}
              <Button
                type="button"
                variant="default"
                loading={test.isPending}
                disabled={!testable}
                onClick={handleTest}
              >
                Test
              </Button>
              <Button
                onClick={handleContinue}
                loading={create.isPending || settings.isPending}
              >
                {touched ? "Continue" : `Continue without ${meta.label}`}
              </Button>
            </Group>
          </Group>
        </Stack>
      }
    >
      <TextInput
        label="Name"
        value={name}
        onChange={(e) => setName(e.currentTarget.value)}
      />

      <Group gap="md" align="flex-start" wrap="nowrap">
        <TextInput
          label="Address"
          description="Hostname or IP only, no http:// and no port"
          placeholder={`${kind} or 192.168.1.10`}
          style={{ flex: 1 }}
          value={ip}
          error={errors.ip}
          // A paste is split as it lands, and anything typed is split once
          // the reader leaves the field. Splitting on every keystroke took the
          // scheme off the moment it was complete, and the port and path typed
          // after it then stayed in Address.
          onPaste={() => {
            pasted.current = true;
          }}
          onChange={(e) => {
            const value = e.currentTarget.value;
            clearError("ip");
            // Any hand edit makes the note about an address that is no longer
            // the one on screen; a paste that splits writes a new one below.
            setSplitNote(null);
            if (pasted.current) {
              pasted.current = false;
              if (applySplit(value)) {
                return;
              }
            }
            setIp(value);
          }}
          onBlur={(e) => {
            applySplit(e.currentTarget.value);
          }}
        />
        <NumberInput
          label="Port"
          w={110}
          min={1}
          max={65535}
          allowDecimal={false}
          hideControls
          value={port}
          error={errors.port}
          onChange={(value) => {
            setPort(value);
            clearError("port");
          }}
        />
      </Group>

      {splitNote && (
        <Alert color="yellow" title="Moved into the right fields">
          {splitNote}
        </Alert>
      )}

      <TextInput
        label="Base URL"
        description={`Leave empty unless ${meta.label} is served under a path`}
        placeholder={`/${kind}`}
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.currentTarget.value)}
      />

      <Switch
        label="Use SSL"
        checked={ssl}
        onChange={(e) => setSsl(e.currentTarget.checked)}
      />

      <PasswordInput
        label="API key"
        description={`Found in ${meta.label} under Settings, General`}
        placeholder="API key"
        autoComplete="new-password"
        value={apiKey}
        error={errors.apiKey}
        onChange={(e) => {
          setApiKey(e.currentTarget.value);
          clearError("apiKey");
        }}
      />
    </StepLayout>
  );
};

export default ArrStep;
