import { FunctionComponent, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Group,
  Stack,
  Switch,
  Text,
} from "@mantine/core";
import {
  readRefusal,
  useDeleteConnection,
  useReorderConnections,
  useUpdateConnection,
} from "@/apis/hooks/translation";
import { SortableList } from "@/components/plugin-settings";
import type {
  TranslationConnection,
  TranslationEngine,
} from "@/types/translation";
import ConnectionEditor from "./ConnectionEditor";

const connectionName = (
  engine: TranslationEngine,
  item: TranslationConnection,
) =>
  `${engine.schema?.backend_types[item.backend_type]?.label ?? item.backend_type}${
    item.model ? `, ${item.model}` : ""
  }`;

/** The engine's ordered back ends: drag to reorder, a switch each, edit and remove. */
const ConnectionList: FunctionComponent<{ engine: TranslationEngine }> = ({
  engine,
}) => {
  const [editing, setEditing] = useState<TranslationConnection | null>(null);
  const [adding, setAdding] = useState(false);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [problem, setProblem] = useState("");
  const reorder = useReorderConnections();
  const update = useUpdateConnection();
  const remove = useDeleteConnection();
  const note = (error: unknown) =>
    setProblem(readRefusal(error)?.message ?? "The request failed. Try again.");

  return (
    <Stack>
      <Text size="sm" c="dimmed">
        Back ends are tried from the top. A job moves to the next one only when
        the first is rate limited, out of quota, signed out or unreachable.
      </Text>
      {problem && (
        <Alert color="red" role="alert">
          {problem}
        </Alert>
      )}
      <SortableList
        items={engine.connections}
        getId={(item) => String(item.id)}
        getName={(item) => connectionName(engine, item)}
        disabled={reorder.isPending}
        onReorder={(ids) =>
          reorder.mutate(
            { instanceId: engine.id, order: ids.map(Number) },
            { onError: note },
          )
        }
        renderItem={(item) => (
          <Group justify="space-between" wrap="nowrap">
            <Stack gap={2}>
              <Text size="sm" fw={500}>
                {connectionName(engine, item)}
              </Text>
              {item.awaiting_secrets && (
                <Badge color="yellow" variant="light">
                  Needs its key again
                </Badge>
              )}
            </Stack>
            <Group gap="xs" wrap="nowrap">
              <Switch
                aria-label={`Use ${connectionName(engine, item)}`}
                checked={item.enabled}
                onChange={(event) =>
                  update.mutate(
                    {
                      id: item.id,
                      body: { enabled: event.currentTarget.checked },
                    },
                    { onError: note },
                  )
                }
              />
              <Button
                size="compact-sm"
                variant="default"
                onClick={() => setEditing(item)}
              >
                Edit
              </Button>
              {confirming === item.id ? (
                <Button
                  size="compact-sm"
                  color="red"
                  onClick={() => {
                    setConfirming(null);
                    remove.mutate(item.id, { onError: note });
                  }}
                >
                  Confirm remove
                </Button>
              ) : (
                <Button
                  size="compact-sm"
                  variant="subtle"
                  color="red"
                  onClick={() => setConfirming(item.id)}
                >
                  Remove
                </Button>
              )}
            </Group>
          </Group>
        )}
      />
      {engine.connections.length === 0 && (
        <Text size="sm" c="dimmed">
          No back ends yet. Add one to start translating.
        </Text>
      )}
      <Group>
        <Button variant="light" onClick={() => setAdding(true)}>
          Add a back end
        </Button>
      </Group>
      {(adding || editing) && (
        <ConnectionEditor
          engine={engine}
          connection={editing}
          onClose={() => {
            setAdding(false);
            setEditing(null);
          }}
        />
      )}
    </Stack>
  );
};

export default ConnectionList;
