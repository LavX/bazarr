/* eslint-disable camelcase -- transport names. */
import { FunctionComponent } from "react";
import {
  Alert,
  Badge,
  Card,
  Container,
  Group,
  Loader,
  Select,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { useAppTitle } from "@/apis/hooks/site";
import {
  readRefusal,
  usePointersMutation,
  useTranslationState,
} from "@/apis/hooks/translation";
import type { TranslationEngine, TranslationNotice } from "@/types/translation";
import ConnectionList from "./ConnectionList";
import EngineSettings from "./EngineSettings";

const NONE = "none";

const engineLabel = (engine: TranslationEngine) =>
  `${engine.name || engine.plugin?.name || engine.plugin_id}${
    engine.official ? " (Official)" : ""
  }`;

// The official engine first, the rest in the order they were installed.
const ordered = (engines: TranslationEngine[]) =>
  [...engines].sort(
    (a, b) => Number(b.official) - Number(a.official) || a.id - b.id,
  );

const Notices: FunctionComponent<{ notices: TranslationNotice[] }> = ({
  notices,
}) => (
  <>
    {notices.map((notice) => (
      <Alert key={notice.code + String(notice.instance_id)} color="yellow">
        {notice.message}
      </Alert>
    ))}
  </>
);

const EnginePickers: FunctionComponent<{
  engines: TranslationEngine[];
  engine: number | null;
  fallback: number | null;
}> = ({ engines, engine, fallback }) => {
  const pointers = usePointersMutation();
  const data = (except: number | null) => [
    { value: NONE, label: "None" },
    ...ordered(engines)
      .filter((item) => item.id !== except)
      .map((item) => ({ value: String(item.id), label: engineLabel(item) })),
  ];
  const toId = (value: string | null) =>
    value === null || value === NONE ? null : Number(value);
  return (
    <Stack>
      <Group grow align="flex-start">
        <Select
          label="Translation engine"
          description="Used for every translation unless you pick another."
          data={data(null)}
          value={engine === null ? NONE : String(engine)}
          allowDeselect={false}
          onChange={(next) => pointers.mutate({ engine: toId(next) })}
        />
        <Select
          label="Fallback engine"
          description="Takes a job the engine declines."
          data={data(engine)}
          value={fallback === null ? NONE : String(fallback)}
          allowDeselect={false}
          onChange={(next) => pointers.mutate({ fallback_engine: toId(next) })}
        />
      </Group>
      <Notices notices={pointers.data?.notices ?? []} />
      {pointers.error && (
        <Alert color="red" role="alert">
          {readRefusal(pointers.error)?.message ??
            "The choice could not be saved."}
        </Alert>
      )}
    </Stack>
  );
};

/** Settings > Translation. It exists only while the v2 translation runtime is switched on. */
const SettingsTranslationView: FunctionComponent = () => {
  const title = useAppTitle();
  useDocumentTitle(`Translation - Settings - ${title}`);
  const { data, isLoading, error } = useTranslationState();

  if (isLoading) {
    return (
      <Container py="xl">
        <Loader />
      </Container>
    );
  }
  if (error || !data) {
    return (
      <Container py="xl">
        <Alert color="red" role="alert">
          The translation settings could not be loaded.
        </Alert>
      </Container>
    );
  }

  return (
    <Container size="md" py="md">
      <Stack gap="lg">
        <Title order={2}>Translation</Title>
        <EnginePickers
          engines={data.instances}
          engine={data.engine}
          fallback={data.fallback_engine}
        />
        {data.instances.length === 0 && (
          <Text c="dimmed">
            No translation engine is installed yet. Install one from the Plugin
            Hub.
          </Text>
        )}
        {ordered(data.instances).map((engine) => (
          <Card key={engine.id} withBorder padding="lg">
            <Stack>
              <Group justify="space-between">
                <Title order={3} size="h4">
                  {engineLabel(engine)}
                </Title>
                <Group gap="xs">
                  {engine.plugin?.version && (
                    <Badge variant="light">v{engine.plugin.version}</Badge>
                  )}
                  <Badge color={engine.configured ? "green" : "gray"}>
                    {engine.configured ? "Ready" : "Not set up"}
                  </Badge>
                </Group>
              </Group>
              {!engine.plugin?.active && (
                <Alert color="yellow">
                  The plugin for this engine is not installed or not running.
                </Alert>
              )}
              {(engine.schema?.fields.length ?? 0) > 0 && (
                <EngineSettings key={engine.id} engine={engine} />
              )}
              {Object.keys(engine.schema?.backend_types ?? {}).length > 0 && (
                <ConnectionList engine={engine} />
              )}
            </Stack>
          </Card>
        ))}
      </Stack>
    </Container>
  );
};

export default SettingsTranslationView;
