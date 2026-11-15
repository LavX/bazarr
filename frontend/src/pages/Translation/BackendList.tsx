/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useId, useRef, useState } from "react";
import { Alert, Button, Menu, Switch, VisuallyHidden } from "@mantine/core";
import {
  faCircleCheck,
  faPlus,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  afterStateDelivered,
  readRefusal,
  useFreshState,
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
  /** The revision of the back end the test ran against. A changed back end has not been tested. */
  revision: number;
}

/** Why a row's switch cannot be used while that row's form is open. */
const SWITCH_WAITS =
  "Save or close the form to switch this back end on or off.";

const seconds = (ms: number) =>
  ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;

/**
 * The engine's back ends in the order they are tried. Each row can be dragged,
 * moved with the keyboard, switched on and off, tested and opened for editing.
 */
const BackendList: FunctionComponent<{
  engine: TranslationEngine;
  /** Whether the server can send a test request through a back end. */
  canTest?: boolean;
}> = ({ engine, canTest = false }) => {
  // The editor finds its back end by id, so removing it closes the editor. The editor keeps the
  // revision it opened with: a refreshed list does not move its draft on to a newer revision.
  const [editingId, setEditingId] = useState<number | null>(null);
  const [adding, setAdding] = useState<StartingPoint | null>(null);
  // The open form is in the middle of a save, a reload or a removal. Until it is over, no other form
  // can be opened and this one cannot be closed: its answer needs the form it was given in.
  const [formBusy, setFormBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const hintId = useId();
  const [tests, setTests] = useState<Record<number, TestOutcome>>({});
  const [testing, setTesting] = useState<number | null>(null);
  // The order as the user just set it, shown until the saved order comes back.
  const [pendingOrder, setPendingOrder] = useState<string[] | null>(null);
  // The newest order that still has to be saved. A move made while a save is on its way waits here, so
  // the list stays usable and the last order asked for is the one that ends up saved.
  const queued = useRef<{ ids: string[]; said: string } | null>(null);
  const saving = useRef(false);
  // The switches whose write is on its way, with the value they were set to.
  const [switching, setSwitching] = useState<Record<number, boolean>>({});
  const switchingNow = useRef(new Set<number>());
  const fresh = useFreshState();
  const reorder = useReorderConnections();
  const update = useUpdateConnection();
  const test = useTestConnection();

  const note = (error: unknown) => {
    const refusal = readRefusal(error);
    setProblem(
      refusal?.code === "stale_revision"
        ? "That back end was changed somewhere else. The list now shows what is saved; try again."
        : refusal?.message || "The request failed. Try again.",
    );
  };

  const saveOrders = async () => {
    if (saving.current) {
      return;
    }
    saving.current = true;
    try {
      while (queued.current) {
        const next = queued.current;
        queued.current = null;
        try {
          await reorder.mutateAsync({
            instanceId: engine.id,
            order: next.ids.map(Number),
          });
          if (!queued.current) {
            setAnnouncement(next.said);
          }
        } catch (error) {
          // The list goes back to what is saved; a move made meanwhile started from the refused order.
          queued.current = null;
          note(error);
          setAnnouncement("The order was not saved.");
        }
      }
    } finally {
      saving.current = false;
      // The saved order takes over once it is on the page, unless another move came in meanwhile.
      afterStateDelivered(() => {
        if (!saving.current) {
          setPendingOrder(null);
        }
      });
    }
  };

  const setEnabled = async (item: TranslationConnection, enabled: boolean) => {
    // One write per switch at a time: a second press while the first is on its way changes nothing.
    if (switchingNow.current.has(item.id)) {
      return;
    }
    switchingNow.current.add(item.id);
    setSwitching((current) => ({ ...current, [item.id]: enabled }));
    setProblem("");
    try {
      await update.mutateAsync({
        id: item.id,
        body: { enabled, expected_revision: item.revision },
      });
    } catch (error) {
      note(error);
      // The row stays held until the page has what is saved now: released earlier, the switch and
      // the form would work from the revision that was just refused.
      await fresh().catch(() => undefined);
    } finally {
      afterStateDelivered(() => {
        switchingNow.current.delete(item.id);
        setSwitching((current) => {
          const rest = { ...current };
          delete rest[item.id];
          return rest;
        });
      });
    }
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
    // The answer belongs to the back end as it was when the test ran, and is said aloud as well as shown.
    // Emptied first: a result that reads the same as the last one is then a change, and is said again.
    setAnnouncement("");
    const report = (ok: boolean, message: string) => {
      setTests((current) => ({
        ...current,
        [item.id]: { ok, message, revision: item.revision },
      }));
      setAnnouncement(backendName(engine, item) + ": " + message);
    };
    setTesting(item.id);
    test.mutate(item.id, {
      onSuccess: (result) =>
        report(
          result.ok,
          result.ok
            ? "It works." +
                (typeof result.latency_ms === "number"
                  ? " Answered in " + seconds(result.latency_ms) + "."
                  : "")
            : result.message,
        ),
      onError: (error) =>
        report(
          false,
          readRefusal(error)?.message ||
            "The test could not run. Check that Bazarr+ is reachable and try again.",
        ),
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
        rowClassName={styles.backendRow}
        onReorder={(ids, moved) => {
          const target = items.find((item) => String(item.id) === moved.id);
          setPendingOrder(ids);
          setProblem("");
          queued.current = {
            ids,
            said: target
              ? `${backendName(engine, target)} moved to position ${moved.index + 1} of ${ids.length}.`
              : "",
          };
          void saveOrders();
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
                {canTest && (
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
                )}
                <Button
                  size="compact-sm"
                  variant={editing ? "light" : "default"}
                  aria-expanded={editing}
                  aria-label={`Edit ${name}`}
                  // Not while this row's own switch is being written either: the form would open on the
                  // revision the switch is about to leave behind.
                  disabled={formBusy || item.id in switching}
                  onClick={() => {
                    setAdding(null);
                    setEditingId(editing ? null : item.id);
                  }}
                >
                  {editing ? "Close" : "Edit"}
                </Button>
                <Switch
                  aria-label={`Use ${name}`}
                  checked={switching[item.id] ?? item.enabled}
                  aria-busy={item.id in switching}
                  // The open form belongs to the revision it was opened with, and the switch would move
                  // the back end on to the next one: the form's save would then be refused as a conflict
                  // with this page's own write.
                  disabled={editing}
                  aria-describedby={editing ? hintId + item.id : undefined}
                  title={editing ? SWITCH_WAITS : undefined}
                  onChange={(event) =>
                    void setEnabled(item, event.currentTarget.checked)
                  }
                />
                {editing && (
                  <VisuallyHidden id={hintId + item.id}>
                    {SWITCH_WAITS}
                  </VisuallyHidden>
                )}
              </div>
            </div>
          );
        }}
        renderDetail={(item) => {
          // A result is shown only for the back end it was run against: any change since makes it old.
          const outcome =
            tests[item.id]?.revision === item.revision
              ? tests[item.id]
              : undefined;
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
                    onBusy={setFormBusy}
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
            onBusy={setFormBusy}
          />
        </div>
      ) : (
        <Menu position="bottom-start" withinPortal>
          <Menu.Target>
            <Button
              mt="sm"
              variant="light"
              disabled={formBusy}
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
