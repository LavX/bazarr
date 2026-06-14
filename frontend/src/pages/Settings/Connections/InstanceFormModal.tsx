import { FunctionComponent, useEffect, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Divider,
  Group,
  Modal,
  NumberInput,
  PasswordInput,
  SegmentedControl,
  Stack,
  Switch,
  Text,
  TextInput,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { showNotification } from "@mantine/notifications";
import {
  faCircleCheck,
  faCircleXmark,
  faPlugCircleBolt,
  faShieldHalved,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useCreateArrInstance,
  useTestArrInstanceById,
  useTestArrInstanceConnection,
  useUpdateArrInstance,
} from "@/apis/hooks";
import type {
  ArrInstance,
  ArrInstanceTest,
  ArrInstanceUpdate,
  ArrKind,
} from "@/apis/raw/arrInstances";
import {
  ARR_META,
  buildArrInstanceCreateBody,
  normalizeBaseUrl,
  stripLeadingSlash,
} from "./meta";

// How a stored API key is treated when editing. The key itself is never sent
// to the browser, so the form only ever carries a brand new value.
type KeyMode = "keep" | "replace" | "clear";

interface FormValues {
  kind: ArrKind;
  name: string;
  ip: string;
  port: number | string;
  baseUrl: string;
  ssl: boolean;
  verifySsl: boolean;
  httpTimeout: number | string;
  apiKey: string;
  enabled: boolean;
  isDefault: boolean;
}

function initialValues(
  kind: ArrKind,
  instance: ArrInstance | null,
): FormValues {
  if (instance) {
    return {
      kind: instance.kind,
      name: instance.name,
      ip: instance.ip,
      port: instance.port,
      baseUrl: stripLeadingSlash(instance.base_url),
      ssl: instance.ssl,
      verifySsl: instance.verify_ssl,
      httpTimeout: instance.http_timeout,
      apiKey: "",
      enabled: instance.enabled,
      isDefault: instance.is_default,
    };
  }
  return {
    kind,
    name: "",
    ip: "",
    port: ARR_META[kind].defaultPort,
    baseUrl: "",
    ssl: false,
    verifySsl: true,
    httpTimeout: 60,
    apiKey: "",
    enabled: true,
    isDefault: false,
  };
}

interface Props {
  opened: boolean;
  kind: ArrKind;
  instance: ArrInstance | null;
  onClose: () => void;
}

const InstanceFormModal: FunctionComponent<Props> = ({
  opened,
  kind,
  instance,
  onClose,
}) => {
  const create = useCreateArrInstance();
  const update = useUpdateArrInstance();
  // Two test paths: `test` sends a typed key from the body (create/replace);
  // `testById` uses the instance's stored key server-side (keep current key).
  const test = useTestArrInstanceConnection();
  const testById = useTestArrInstanceById();

  const [keyMode, setKeyMode] = useState<KeyMode>("keep");
  const hasStoredKey = instance !== null && instance.api_key_set;

  const resetTests = () => {
    test.reset();
    testById.reset();
  };

  const form = useForm<FormValues>({
    initialValues: initialValues(kind, instance),
    validate: {
      name: (value) => (value.trim().length ? null : "Name is required"),
      ip: (value) => (value.trim().length ? null : "Address is required"),
      port: (value) => {
        const port = Number(value);
        return Number.isInteger(port) && port >= 1 && port <= 65535
          ? null
          : "Port must be between 1 and 65535";
      },
      httpTimeout: (value) => {
        const timeout = Number(value);
        return Number.isInteger(timeout) && timeout >= 1
          ? null
          : "Timeout must be at least 1 second";
      },
    },
    // Any change makes a previous test result stale, so drop it.
    onValuesChange: () => resetTests(),
  });

  useEffect(() => {
    if (opened) {
      form.setValues(initialValues(kind, instance));
      form.resetDirty();
      form.clearErrors();
      setKeyMode("keep");
      resetTests();
    }
    // The form and test objects are recreated every render; syncing on them
    // would loop. This effect only needs to run when the modal opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, kind, instance]);

  const meta = ARR_META[form.values.kind];

  const changeKind = (next: ArrKind) => {
    const previousDefault = ARR_META[form.values.kind].defaultPort;
    if (form.values.port === previousDefault || form.values.port === "") {
      form.setFieldValue("port", ARR_META[next].defaultPort);
    }
    form.setFieldValue("kind", next);
  };

  // The typed key is included unless the user chose to keep or clear a stored
  // one. Stored keys never leave the server, so they cannot be re-sent here.
  const typedApiKey = (): string | undefined => {
    if (hasStoredKey && keyMode !== "replace") {
      return undefined;
    }
    const typed = form.values.apiKey.trim();
    return typed.length ? typed : undefined;
  };

  const runTest = () => {
    const checks = [
      form.validateField("ip"),
      form.validateField("port"),
      form.validateField("httpTimeout"),
    ];
    if (checks.some((check) => check.hasError)) {
      return;
    }
    const values = form.values;
    const overrides = {
      ip: values.ip.trim(),
      port: Number(values.port),
      base_url: normalizeBaseUrl(values.baseUrl),
      ssl: values.ssl,
      verify_ssl: values.verifySsl,
      http_timeout: Number(values.httpTimeout),
    };
    // Keeping the stored key: test server-side with that key (it never reaches
    // the browser), using the on-screen connection values as overrides.
    if (instance && hasStoredKey && keyMode === "keep") {
      test.reset();
      testById.mutate({ id: instance.id, overrides });
      return;
    }
    // Creating, replacing, or clearing the key: test with the typed key (if
    // any) straight from the body.
    testById.reset();
    const body: ArrInstanceTest = { kind: values.kind, ...overrides };
    const key = typedApiKey();
    if (key) {
      body.api_key = key;
    }
    test.mutate(body);
  };

  const testResult = testById.data ?? test.data;
  const testPending = test.isPending || testById.isPending;
  const testErrored = test.isError || testById.isError;

  const saving = create.isPending || update.isPending;

  const submit = form.onSubmit((values) => {
    const common = {
      name: values.name.trim(),
      ip: values.ip.trim(),
      port: Number(values.port),
      base_url: normalizeBaseUrl(values.baseUrl),
      ssl: values.ssl,
      verify_ssl: values.verifySsl,
      http_timeout: Number(values.httpTimeout),
      enabled: values.enabled,
    };
    const typed = values.apiKey.trim();
    if (instance) {
      const body: ArrInstanceUpdate = {
        ...common,
        is_default: values.isDefault,
      };
      if (hasStoredKey) {
        if (keyMode === "replace" && typed.length) {
          body.api_key = typed;
        }
        if (keyMode === "clear") {
          body.clear_api_key = true;
        }
      } else if (typed.length) {
        body.api_key = typed;
      }
      update.mutate(
        { id: instance.id, body },
        {
          onSuccess: () => {
            showNotification({
              color: "green",
              message: `Instance "${common.name}" updated`,
            });
            onClose();
          },
        },
      );
    } else {
      const body = buildArrInstanceCreateBody(values.kind, {
        name: common.name,
        ip: common.ip,
        port: common.port,
        base_url: common.base_url,
        ssl: common.ssl,
        verify_ssl: common.verify_ssl,
        http_timeout: common.http_timeout,
        enabled: common.enabled,
        isDefault: values.isDefault,
        apiKey: typed.length ? typed : undefined,
      });
      create.mutate(body, {
        onSuccess: () => {
          showNotification({
            color: "green",
            message: `Instance "${common.name}" added`,
          });
          onClose();
        },
      });
    }
  });

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={
        instance
          ? `Edit ${instance.name}`
          : `Add ${ARR_META[form.values.kind].label} instance`
      }
      size="lg"
      centered
    >
      <form onSubmit={submit}>
        <Stack gap="md">
          {!instance && (
            <SegmentedControl
              fullWidth
              value={form.values.kind}
              onChange={(value) => changeKind(value as ArrKind)}
              data={(["sonarr", "radarr"] as const).map((value) => ({
                value,
                label: (
                  <Group gap={8} justify="center" wrap="nowrap">
                    <FontAwesomeIcon icon={ARR_META[value].icon} />
                    <span>{ARR_META[value].label}</span>
                  </Group>
                ),
              }))}
            />
          )}

          <TextInput
            label="Name"
            placeholder={`e.g. Main ${meta.label}`}
            required
            data-autofocus
            {...form.getInputProps("name")}
          />

          <Group gap="md" align="flex-start" wrap="nowrap">
            <TextInput
              label="Address"
              description="Hostname or IPv4 address"
              placeholder="127.0.0.1"
              required
              style={{ flex: 1 }}
              {...form.getInputProps("ip")}
            />
            <NumberInput
              label="Port"
              w={110}
              min={1}
              max={65535}
              allowDecimal={false}
              hideControls
              required
              {...form.getInputProps("port")}
            />
          </Group>

          <Group gap="md" align="flex-start" wrap="nowrap">
            <TextInput
              label="Base URL"
              description="Only needed behind a reverse proxy"
              leftSection={
                <Text size="sm" c="dimmed">
                  /
                </Text>
              }
              placeholder={form.values.kind}
              style={{ flex: 1 }}
              {...form.getInputProps("baseUrl")}
            />
            <NumberInput
              label="Timeout"
              description="Seconds"
              w={110}
              min={1}
              allowDecimal={false}
              {...form.getInputProps("httpTimeout")}
            />
          </Group>

          <Group gap="xl">
            <Switch
              label="Use SSL"
              {...form.getInputProps("ssl", { type: "checkbox" })}
            />
            <Switch
              label="Verify certificate"
              disabled={!form.values.ssl}
              {...form.getInputProps("verifySsl", { type: "checkbox" })}
            />
          </Group>

          {hasStoredKey ? (
            <Stack gap={6}>
              <Group gap="xs">
                <Text size="sm" fw={500}>
                  API Key
                </Text>
                <Badge
                  size="xs"
                  variant="light"
                  color="green"
                  leftSection={<FontAwesomeIcon icon={faShieldHalved} />}
                >
                  Stored
                </Badge>
              </Group>
              <SegmentedControl
                size="xs"
                value={keyMode}
                onChange={(value) => {
                  setKeyMode(value as KeyMode);
                  resetTests();
                }}
                data={[
                  { value: "keep", label: "Keep current key" },
                  { value: "replace", label: "Replace" },
                  { value: "clear", label: "Clear" },
                ]}
              />
              {keyMode === "keep" && (
                <Text size="xs" c="dimmed">
                  The stored key never leaves the server, so it is not shown
                  here. Connection tests use it automatically.
                </Text>
              )}
              {keyMode === "replace" && (
                <PasswordInput
                  placeholder="Enter the new API key"
                  aria-label="New API key"
                  autoComplete="new-password"
                  {...form.getInputProps("apiKey")}
                />
              )}
              {keyMode === "clear" && (
                <Text size="xs" c="orange">
                  The stored key will be removed when you save.
                </Text>
              )}
            </Stack>
          ) : (
            <PasswordInput
              label="API Key"
              description={
                instance
                  ? "No key is stored yet. Enter one to authenticate."
                  : `Found in ${meta.label} under Settings, General`
              }
              placeholder="API key"
              autoComplete="new-password"
              {...form.getInputProps("apiKey")}
            />
          )}

          <Divider />

          <Group gap="xl">
            <Switch
              label="Enabled"
              {...form.getInputProps("enabled", { type: "checkbox" })}
            />
            <Switch
              label={`Default ${meta.label} instance`}
              {...form.getInputProps("isDefault", { type: "checkbox" })}
            />
          </Group>

          <Divider label="Connection check" labelPosition="left" />

          <Group justify="space-between" align="center" wrap="nowrap">
            <Text size="xs" c="dimmed">
              Tests the values above without saving anything.
            </Text>
            <Button
              type="button"
              variant="light"
              leftSection={<FontAwesomeIcon icon={faPlugCircleBolt} />}
              loading={testPending}
              onClick={runTest}
            >
              Test Connection
            </Button>
          </Group>

          {testResult &&
            (testResult.ok ? (
              <Alert
                color="green"
                icon={<FontAwesomeIcon icon={faCircleCheck} />}
                title={`Connected to ${testResult.app_name ?? meta.label}`}
              >
                {testResult.version
                  ? `Version ${testResult.version}`
                  : "The instance responded successfully."}
              </Alert>
            ) : (
              <Alert
                color="red"
                icon={<FontAwesomeIcon icon={faCircleXmark} />}
                title="Connection failed"
              >
                {testResult.message ??
                  testResult.error ??
                  "The instance did not respond."}
              </Alert>
            ))}
          {testErrored && (
            <Alert
              color="red"
              icon={<FontAwesomeIcon icon={faCircleXmark} />}
              title="Test failed"
            >
              Could not reach the Bazarr API to run the connection test.
            </Alert>
          )}

          <Group justify="flex-end" mt="xs">
            <Button type="button" variant="default" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" loading={saving}>
              {instance ? "Save changes" : "Add instance"}
            </Button>
          </Group>
        </Stack>
      </form>
    </Modal>
  );
};

export default InstanceFormModal;
