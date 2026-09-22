import { FC, useState } from "react";
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
  Title,
} from "@mantine/core";
import {
  getArrInstanceErrorMessage,
  useArrInstances,
  useCreateArrInstance,
  useDeleteArrInstance,
  useSettingsMutation,
  useTestArrInstanceConnection,
} from "@/apis/hooks";
import type {
  ArrInstanceCreate,
  ArrInstanceTest,
} from "@/apis/raw/arrInstances";
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

function normalizeBaseUrl(value: string) {
  const trimmed = value.trim().replace(/^\/+/, "").replace(/\/+$/, "");
  return trimmed ? `/${trimmed}` : "";
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
 * advances. Idempotent: if an instance of this kind already exists, it shows a
 * connected state and Continue advances without creating a duplicate.
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
const ArrStep: FC<ArrStepProps> = ({ kind, onNext, onBack }) => {
  const meta = KIND_META[kind];

  const { data: instances } = useArrInstances();
  const create = useCreateArrInstance();
  const remove = useDeleteArrInstance();
  const settings = useSettingsMutation();

  const existing = (instances ?? []).find((i) => i.kind === kind) ?? null;

  const [name, setName] = useState(`Main ${meta.label}`);
  const [ip, setIp] = useState("");
  const [port, setPort] = useState<number | string>(meta.port);
  const [baseUrl, setBaseUrl] = useState("");
  const [ssl, setSsl] = useState(false);
  const [apiKey, setApiKey] = useState("");
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

  const test = useTestArrInstanceConnection({
    ip: trimmedIp,
    port: portNumber,
    baseUrl: normalizedBaseUrl,
    ssl,
    apiKey: trimmedKey,
  });

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
    remove.mutate(existing.id, {
      onSuccess: () => {
        setConfirmRemove(false);
        // The row this message was about is gone, so the message goes with it,
        // and so does the memory of having written it.
        setSaveError(null);
        setCreatedId(null);
      },
      onError: (error) =>
        setRemoveError(
          getArrInstanceErrorMessage(
            error,
            `Bazarr+ could not remove ${existing.name}. You can edit it in Settings, Connections after setup.`,
          ),
        ),
    });
  };

  const testResult = test.data;

  if (existing) {
    const address = `${existing.ssl ? "https" : "http"}://${existing.ip}:${
      existing.port
    }${existing.base_url && existing.base_url !== "/" ? existing.base_url : ""}`;
    return (
      <Stack gap="lg">
        <Stack gap="xs">
          <Title order={2}>{meta.label}</Title>
          <Text c="dimmed">
            Connect {meta.label} for your {meta.media}.
          </Text>
        </Stack>
        <Alert color="green" title="Already connected">
          <Stack gap="sm" align="flex-start">
            <Text size="sm">
              {existing.name} at {address}
            </Text>
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
        <Group justify="space-between">
          <Group gap="sm">
            {onBack && (
              <Button variant="default" onClick={onBack}>
                Back
              </Button>
            )}
          </Group>
          <Button onClick={handleContinue}>Continue</Button>
        </Group>
      </Stack>
    );
  }

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={2}>{meta.label}</Title>
        <Text c="dimmed">
          Connect {meta.label} so Bazarr can find your {meta.media}.
        </Text>
      </Stack>

      <TextInput
        label="Name"
        value={name}
        onChange={(e) => setName(e.currentTarget.value)}
      />

      <Group gap="md" align="flex-start" wrap="nowrap">
        <TextInput
          label="Address"
          placeholder="127.0.0.1"
          style={{ flex: 1 }}
          value={ip}
          error={errors.ip}
          onChange={(e) => {
            setIp(e.currentTarget.value);
            clearError("ip");
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

      <TextInput
        label="Base URL"
        description="Only needed behind a reverse proxy"
        placeholder={kind}
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.currentTarget.value)}
      />

      <Switch
        label="Use SSL"
        checked={ssl}
        onChange={(e) => setSsl(e.currentTarget.checked)}
      />

      <PasswordInput
        label="API Key"
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

      <Group>
        {/* An ungated Test probed 127.0.0.1 on the default port with an empty
            key and reported a verdict about a server the reader never named. */}
        <Button
          type="button"
          variant="light"
          loading={test.isPending}
          disabled={!testable}
          onClick={handleTest}
        >
          Test
        </Button>
      </Group>

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
      {saveError && (
        <Alert color="red" title={`Could not connect ${meta.label}`}>
          {saveError}
        </Alert>
      )}

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
          loading={create.isPending || settings.isPending}
        >
          {touched ? "Continue" : `Continue without ${meta.label}`}
        </Button>
      </Group>
    </Stack>
  );
};

export default ArrStep;
