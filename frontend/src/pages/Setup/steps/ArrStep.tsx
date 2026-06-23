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
  useArrInstances,
  useCreateArrInstance,
  useSettingsMutation,
  useTestArrInstanceConnection,
} from "@/apis/hooks";
import type {
  ArrInstanceCreate,
  ArrInstanceTest,
  ArrKind,
} from "@/apis/raw/arrInstances";
import type { WizardStepProps } from "./types";

export interface ArrStepProps extends WizardStepProps {
  kind: ArrKind;
  required?: boolean;
}

const KIND_META: Record<
  ArrKind,
  { label: string; media: string; port: number }
> = {
  sonarr: { label: "Sonarr", media: "TV shows", port: 8989 },
  radarr: { label: "Radarr", media: "movies", port: 7878 },
};

function normalizeBaseUrl(value: string) {
  const trimmed = value.trim().replace(/^\/+/, "").replace(/\/+$/, "");
  return trimmed ? `/${trimmed}` : "";
}

/**
 * Onboarding connection step for a single arr kind. Mirrors the InstanceFormModal
 * field set with bespoke inputs (the modal is not reused here). Tests the typed
 * connection, then on Continue creates the instance, flips use_<kind> on, and
 * advances. Idempotent: if an instance of this kind already exists, it shows a
 * connected state and Continue advances without creating a duplicate.
 */
const ArrStep: FC<ArrStepProps> = ({ kind, required, onNext }) => {
  const meta = KIND_META[kind];

  const { data: instances } = useArrInstances();
  const create = useCreateArrInstance();
  const test = useTestArrInstanceConnection();
  const settings = useSettingsMutation();

  const existing = (instances ?? []).find((i) => i.kind === kind) ?? null;

  const [name, setName] = useState(`Main ${meta.label}`);
  const [ip, setIp] = useState("");
  const [port, setPort] = useState<number | string>(meta.port);
  const [baseUrl, setBaseUrl] = useState("");
  const [ssl, setSsl] = useState(false);
  const [apiKey, setApiKey] = useState("");

  const buildOverrides = () => ({
    ip: ip.trim(),
    port: Number(port),
    base_url: normalizeBaseUrl(baseUrl),
    ssl,
    verify_ssl: true,
    http_timeout: 60,
  });

  const handleTest = () => {
    const body: ArrInstanceTest = { kind, ...buildOverrides() };
    const key = apiKey.trim();
    if (key) {
      body.api_key = key;
    }
    test.mutate(body);
  };

  const handleContinue = () => {
    if (existing) {
      onNext();
      return;
    }
    const body: ArrInstanceCreate = {
      kind,
      name: name.trim(),
      ...buildOverrides(),
      enabled: true,
      is_default: true,
    };
    const key = apiKey.trim();
    if (key) {
      body.api_key = key;
    }
    create.mutate(body, {
      onSuccess: () => {
        settings.mutate({ [`settings-general-use_${kind}`]: true });
        onNext();
      },
    });
  };

  const testResult = test.data;

  if (existing) {
    return (
      <Stack gap="lg">
        <Stack gap="xs">
          <Title order={2}>{meta.label}</Title>
          <Text c="dimmed">
            Connect {meta.label} for your {meta.media}.
          </Text>
        </Stack>
        <Alert color="green" title="Already connected">
          {existing.name}
        </Alert>
        <Group justify="flex-end">
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
          onChange={(e) => setIp(e.currentTarget.value)}
        />
        <NumberInput
          label="Port"
          w={110}
          min={1}
          max={65535}
          allowDecimal={false}
          hideControls
          value={port}
          onChange={setPort}
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
        onChange={(e) => setApiKey(e.currentTarget.value)}
      />

      <Group>
        <Button
          type="button"
          variant="light"
          loading={test.isPending}
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

      <Group justify="space-between">
        {required ? (
          <Button variant="subtle" color="gray" onClick={onNext}>
            Skip for now
          </Button>
        ) : (
          <Button variant="subtle" color="gray" onClick={onNext}>
            Skip
          </Button>
        )}
        <Button onClick={handleContinue} loading={create.isPending}>
          Continue
        </Button>
      </Group>
    </Stack>
  );
};

export default ArrStep;
