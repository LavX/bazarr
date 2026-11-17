/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useId, useRef, useState } from "react";
import { Alert, Button, Menu, Switch, VisuallyHidden } from "@mantine/core";
import {
  faCircleCheck,
  faClock,
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
import AttentionNotice from "./AttentionNotice";
import BackendEditor from "./BackendEditor";
import {
  backendHold,
  backendLabel,
  backendName,
  backendTypes,
  keyState,
  pricePair,
  StartingPoint,
  startingPoints,
} from "./model";
import styles from "./Translation.module.scss";

interface TestOutcome {
  /**
   * How the answer is shown. `waiting` is a test that never ran because the engine could not take it
   * just now: nothing is wrong with the back end, so it is not shown as a failure.
   */
  tone: "success" | "danger" | "waiting";
  message: string;
  /** The revision of the back end the test ran against. A changed back end has not been tested. */
  revision: number;
}

const RESULT_ICON = {
  success: faCircleCheck,
  danger: faTriangleExclamation,
  waiting: faClock,
};

/** Why a row's switch cannot be used while that row's form is open. */
const SWITCH_WAITS =
  "Save or close the form to switch this back end on or off.";

/**
 * Why a held-back back end has no test to run. A test uses what is saved, and what is saved is what
 * no longer fits: the server would answer about a changed address, which is not what happened.
 */
const TEST_WAITS =
  "A test uses what is saved. Check and save this back end's settings first.";
const TEST_GONE =
  "Nothing to test: the engine's plugin no longer has this kind of back end.";

/** What stands before the reason of a test that never ran, so it is not read as the back end's answer. */
const NOT_TESTED = "Not tested yet.";
const TRY_AGAIN_SOON =
  "The engine could not take the test just now. Try again in a moment.";

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
  // The fields the server named when it refused to turn a back end on, for that back end's form to
  // mark. They belong to the revision that was refused: a save moves the back end on and answers them.
  const [refused, setRefused] = useState<
    Record<number, { revision: number; errors: Record<string, string> }>
  >({});
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
      // Turning a back end on is refused while what it has saved does not fit its type. The server
      // names the fields and its sentence names none, so the page says where to go.
      const refusal = readRefusal(error);
      const shown = (backendTypes(engine)[item.backend_type]?.fields ?? []).map(
        (field) => field.key,
      );
      if (
        enabled &&
        refusal &&
        Object.keys(refusal.fieldErrors).some((key) => shown.includes(key))
      ) {
        setRefused((current) => ({
          ...current,
          [item.id]: { revision: item.revision, errors: refusal.fieldErrors },
        }));
        setProblem(
          `${backendName(engine, item)} cannot be turned on yet: a setting needs fixing. Open it with Edit and save its settings, then turn it on.`,
        );
      } else {
        note(error);
      }
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
    const report = (tone: TestOutcome["tone"], message: string) => {
      setTests((current) => ({
        ...current,
        [item.id]: { tone, message, revision: item.revision },
      }));
      setAnnouncement(backendName(engine, item) + ": " + message);
    };
    setTesting(item.id);
    test.mutate(item.id, {
      onSuccess: (result) => {
        if (result.ok) {
          report(
            "success",
            "It works." +
              (typeof result.latency_ms === "number"
                ? " Answered in " + seconds(result.latency_ms) + "."
                : ""),
          );
        } else if (result.outcome === "not_ready") {
          // Nothing was sent: the engine was busy or had not started. The server's sentence says which.
          report(
            "waiting",
            NOT_TESTED + " " + (result.message || TRY_AGAIN_SOON),
          );
        } else {
          report("danger", result.message);
        }
      },
      onError: (error) => {
        const refusal = readRefusal(error);
        if (refusal?.code === "plugin_unavailable") {
          // The engine's plugin is not running, so nothing was sent: the same as an engine that says
          // so in its answer, and not a failure of this back end.
          report(
            "waiting",
            NOT_TESTED + " " + (refusal.message || TRY_AGAIN_SOON),
          );
          // The page offered a test the server can no longer run. What the server lists now is loaded,
          // so Test goes away here once it is no longer listed, and comes back when it is.
          void fresh().catch(() => undefined);
        } else {
          report(
            "danger",
            refusal?.message ||
              "The test could not run. Check that Bazarr+ is reachable and try again.",
          );
        }
      },
      onSettled: () => setTesting(null),
    });
  };

  const open = (point: StartingPoint) => {
    setEditingId(null);
    setAdding(point);
  };

  // The back ends a plugin update turned off and that can be brought back by checking their settings.
  // The one whose form is open is left out: its form says the same at its top.
  const toReview = items.filter(
    (item) => backendHold(engine, item) === "review" && item.id !== editingId,
  );

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
      {toReview.length > 0 && (
        <AttentionNotice
          style={{ marginBottom: 12 }}
          title={
            toReview.length === 1
              ? "A back end was turned off until its settings are checked"
              : `${toReview.length} back ends were turned off until their settings are checked`
          }
        >
          {toReview.length === 1
            ? `What it has saved no longer fits, usually because a plugin update changed what a back end needs. Open ${backendName(engine, toReview[0])} with Edit, check its settings and save them, then turn it on with its switch.`
            : "What they have saved no longer fits, usually because a plugin update changed what a back end needs. Open each one marked Needs a review with Edit, check its settings and save them, then turn it on with its switch."}
        </AttentionNotice>
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
          const hold = backendHold(engine, item);
          const testWaits =
            hold === "review" ? TEST_WAITS : hold === "gone" ? TEST_GONE : "";
          return (
            <div className={styles.backend} data-off={!item.enabled}>
              <div className={styles.backendIdentity}>
                <span className={styles.position} aria-hidden="true">
                  {index + 1}
                </span>
                <div className={styles.backendText}>
                  <div className={styles.backendName}>
                    <span>{backendLabel(engine, item)}</span>
                    {hold === "review" ? (
                      <StatusBadge tone="warning" label="Needs a review" />
                    ) : hold === "gone" ? (
                      <StatusBadge tone="warning" label="No longer offered" />
                    ) : item.awaiting_secrets ? (
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
                    disabled={
                      (testing !== null && testing !== item.id) ||
                      testWaits !== ""
                    }
                    aria-label={`Test ${name}`}
                    aria-describedby={
                      testWaits ? `${hintId}test${item.id}` : undefined
                    }
                    title={testWaits || undefined}
                    onClick={() => runTest(item)}
                  >
                    Test
                  </Button>
                )}
                {canTest && testWaits && (
                  <VisuallyHidden id={`${hintId}test${item.id}`}>
                    {testWaits}
                  </VisuallyHidden>
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
                  data-tone={outcome.tone}
                  data-testid="test-result"
                >
                  <FontAwesomeIcon
                    icon={RESULT_ICON[outcome.tone]}
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
                    reviewErrors={
                      refused[item.id]?.revision === item.revision
                        ? refused[item.id].errors
                        : undefined
                    }
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
