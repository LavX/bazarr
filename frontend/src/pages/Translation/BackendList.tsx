/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import { Alert, Button, Menu, Switch, VisuallyHidden } from "@mantine/core";
import {
  faCircleCheck,
  faPlus,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  readRefusal,
  useReorderConnections,
  useTestConnection,
  useUpdateConnection,
} from "@/apis/hooks/translation";
import { SortableList } from "@/components/plugin-settings";
import { StatusBadge } from "@/pages/Settings/Providers/hub/components/StatusBadge";
import type {
  TranslationConnection,
  TranslationEngine,
} from "@/types/translation";
import BackendEditor from "./BackendEditor";
import {
  backendLabel,
  backendName,
  keyState,
  pricePair,
  StartingPoint,
  startingPoints,
} from "./model";
import styles from "./Translation.module.scss";

interface TestOutcome {
  ok: boolean;
  message: string;
}

/** Which item a reorder moved, and where it landed (1-based). */
function movedItem(before: string[], after: string[]) {
  const first = before.findIndex((id, index) => id !== after[index]);
  if (first < 0) {
    return null;
  }
  // Moved down: everything after it shifted up by one. Moved up: it took this place.
  const down = after[first] === before[first + 1];
  const id = down ? before[first] : after[first];
  return { id, position: after.indexOf(id) + 1 };
}

const seconds = (ms: number) =>
  ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;

/**
 * The engine's back ends in the order they are tried. Each row can be dragged,
 * moved with the keyboard, switched on and off, tested and opened for editing.
 */
const BackendList: FunctionComponent<{ engine: TranslationEngine }> = ({
  engine,
}) => {
  // The editor finds its back end by id, so removing it closes the editor. The editor keeps the
  // revision it opened with: a refreshed list does not move its draft on to a newer revision.
  const [editingId, setEditingId] = useState<number | null>(null);
  const [adding, setAdding] = useState<StartingPoint | null>(null);
  const [problem, setProblem] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [tests, setTests] = useState<Record<number, TestOutcome>>({});
  const [testing, setTesting] = useState<number | null>(null);
  // The order as the user just set it, shown until the saved order comes back.
  const [pendingOrder, setPendingOrder] = useState<string[] | null>(null);
  const reorder = useReorderConnections();
  const update = useUpdateConnection();
  const test = useTestConnection();

  const note = (error: unknown) => {
    const refusal = readRefusal(error);
    setProblem(
      refusal?.code === "stale_revision"
        ? "That back end was changed somewhere else. The list now shows what is saved; try again."
        : (refusal?.message ?? "The request failed. Try again."),
    );
  };

  const items = pendingOrder
    ? [...engine.connections].sort(
        (a, b) =>
          pendingOrder.indexOf(String(a.id)) -
          pendingOrder.indexOf(String(b.id)),
      )
    : engine.connections;
  const points = startingPoints(engine);
  const plain = points.filter((point) => !point.group);
  const groups = [...new Set(points.map((point) => point.group))].filter(
    Boolean,
  );

  const runTest = (item: TranslationConnection) => {
    setTesting(item.id);
    test.mutate(item.id, {
      onSuccess: (result) =>
        setTests((current) => ({
          ...current,
          [item.id]: {
            ok: result.ok,
            message: result.ok
              ? `It works.${
                  typeof result.latency_ms === "number"
                    ? ` Answered in ${seconds(result.latency_ms)}.`
                    : ""
                }`
              : result.message,
          },
        })),
      onError: (error) => {
        const refusal = readRefusal(error);
        setTests((current) => ({
          ...current,
          [item.id]: {
            ok: false,
            message:
              refusal?.status === 404
                ? "This server cannot test back ends yet."
                : (refusal?.message ??
                  "The test could not run. Check that Bazarr+ is reachable and try again."),
          },
        }));
      },
      onSettled: () => setTesting(null),
    });
  };

  const open = (point: StartingPoint) => {
    setEditingId(null);
    setAdding(point);
  };

  return (
    <div>
      {problem && (
        <Alert
          color="red"
          role="alert"
          mb="sm"
          withCloseButton
          closeButtonLabel="Dismiss"
          onClose={() => setProblem("")}
        >
          {problem}
        </Alert>
      )}
      <SortableList
        items={items}
        getId={(item) => String(item.id)}
        getName={(item) => backendName(engine, item)}
        disabled={reorder.isPending}
        rowClassName={styles.backendRow}
        onReorder={(ids) => {
          const moved = movedItem(
            items.map((item) => String(item.id)),
            ids,
          );
          const target = items.find((item) => String(item.id) === moved?.id);
          setPendingOrder(ids);
          setProblem("");
          reorder.mutate(
            { instanceId: engine.id, order: ids.map(Number) },
            {
              onSuccess: () =>
                setAnnouncement(
                  target && moved
                    ? `${backendName(engine, target)} moved to position ${moved.position} of ${ids.length}.`
                    : "",
                ),
              onError: (error) => {
                note(error);
                setAnnouncement("The order was not saved.");
              },
              onSettled: () => setPendingOrder(null),
            },
          );
        }}
        renderItem={(item, index) => {
          const name = backendName(engine, item);
          const editing = editingId === item.id;
          return (
            <div className={styles.backend} data-off={!item.enabled}>
              <div className={styles.backendIdentity}>
                <span className={styles.position} aria-hidden="true">
                  {index + 1}
                </span>
                <div className={styles.backendText}>
                  <div className={styles.backendName}>
                    <span>{backendLabel(engine, item)}</span>
                    {item.awaiting_secrets ? (
                      <StatusBadge tone="warning" label="Needs its key again" />
                    ) : (
                      item.enabled &&
                      !item.usable && (
                        <StatusBadge tone="warning" label="Not finished" />
                      )
                    )}
                    {!item.enabled && (
                      <StatusBadge tone="neutral" label="Off" />
                    )}
                  </div>
                  <p className={styles.backendMeta}>
                    <span className={styles.model}>
                      {item.model || "No model picked"}
                    </span>
                    <span className={styles.metaItem}>
                      {pricePair(
                        item.values.price_input_per_million,
                        item.values.price_output_per_million,
                      )}
                    </span>
                    <span className={styles.metaItem}>{keyState(item)}</span>
                  </p>
                </div>
              </div>
              <div className={styles.backendActions}>
                <Button
                  size="compact-sm"
                  variant="default"
                  loading={testing === item.id}
                  disabled={testing !== null && testing !== item.id}
                  aria-label={`Test ${name}`}
                  onClick={() => runTest(item)}
                >
                  Test
                </Button>
                <Button
                  size="compact-sm"
                  variant={editing ? "light" : "default"}
                  aria-expanded={editing}
                  aria-label={`Edit ${name}`}
                  onClick={() => {
                    setAdding(null);
                    setEditingId(editing ? null : item.id);
                  }}
                >
                  {editing ? "Close" : "Edit"}
                </Button>
                <Switch
                  aria-label={`Use ${name}`}
                  checked={item.enabled}
                  onChange={(event) => {
                    setProblem("");
                    update.mutate(
                      {
                        id: item.id,
                        body: {
                          enabled: event.currentTarget.checked,
                          expected_revision: item.revision,
                        },
                      },
                      { onError: note },
                    );
                  }}
                />
              </div>
            </div>
          );
        }}
        renderDetail={(item) => {
          const outcome = tests[item.id];
          const editing = editingId === item.id;
          if (!outcome && !editing) {
            return null;
          }
          return (
            <div className={styles.detail}>
              {outcome && (
                <div
                  className={styles.result}
                  data-tone={outcome.ok ? "success" : "danger"}
                  role="status"
                >
                  <FontAwesomeIcon
                    icon={outcome.ok ? faCircleCheck : faTriangleExclamation}
                    style={{ marginTop: 3 }}
                  />
                  <span className={styles.resultText}>{outcome.message}</span>
                </div>
              )}
              {editing && (
                <div style={{ marginTop: outcome ? 12 : 0 }}>
                  <BackendEditor
                    key={item.id}
                    engine={engine}
                    connection={item}
                    onClose={() => setEditingId(null)}
                  />
                </div>
              )}
            </div>
          );
        }}
      />
      <VisuallyHidden role="status" aria-live="polite">
        {announcement}
      </VisuallyHidden>
      {items.length === 0 && !adding && (
        <div className={styles.quiet}>
          <p className={styles.quietTitle}>No back ends yet</p>
          Add one and translation can start. A back end is the service that does
          the translating: OpenRouter, Gemini, or a server of your own.
        </div>
      )}
      {adding ? (
        <div className={styles.newBackend}>
          <h4 className={styles.sectionTitle} style={{ marginBottom: 12 }}>
            New back end: {adding.label}
          </h4>
          <BackendEditor
            key={adding.key}
            engine={engine}
            connection={null}
            start={adding}
            onClose={() => setAdding(null)}
          />
        </div>
      ) : (
        <Menu position="bottom-start" withinPortal>
          <Menu.Target>
            <Button
              mt="sm"
              variant="light"
              leftSection={<FontAwesomeIcon icon={faPlus} />}
            >
              Add a back end
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {plain.map((point) => (
              <Menu.Item key={point.key} onClick={() => open(point)}>
                {point.label}
              </Menu.Item>
            ))}
            {groups.map((group) => (
              <div key={group}>
                <Menu.Label>{group}</Menu.Label>
                {points
                  .filter((point) => point.group === group)
                  .map((point) => (
                    <Menu.Item key={point.key} onClick={() => open(point)}>
                      {point.label}
                    </Menu.Item>
                  ))}
              </div>
            ))}
          </Menu.Dropdown>
        </Menu>
      )}
    </div>
  );
};

export default BackendList;
