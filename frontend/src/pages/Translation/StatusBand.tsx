import { FunctionComponent } from "react";
import { StatusBadge } from "@/pages/Settings/Providers/hub/components/StatusBadge";
import type { TranslationState, TranslationSummary } from "@/types/translation";
import {
  backendName,
  canRun,
  engineName,
  engineStatus,
  hasBackends,
  heldBack,
  money,
  working,
} from "./model";
import styles from "./Translation.module.scss";

/** How full the month is, as the meter colours it. */
function spendTone(spent: number, cap: number | null) {
  if (cap === null || cap <= 0) {
    return "normal";
  }
  const share = spent / cap;
  return share >= 1 ? "danger" : share >= 0.8 ? "warning" : "normal";
}

/**
 * The page's standing answer to "is it working, and what is it costing":
 * the engine and its state, the queue, and the month's spend against its cap.
 * The queue and the spend are left out when the server does not report them.
 */
const StatusBand: FunctionComponent<{
  state: TranslationState;
  summary?: TranslationSummary;
}> = ({ state, summary }) => {
  const engine =
    state.instances.find((item) => item.id === state.engine) ?? null;
  const status = engine ? engineStatus(engine) : null;
  // With no engine picked, an engine that was turned off and waits for a look is the likely reason.
  const waiting = engine
    ? null
    : (state.instances.find((item) => heldBack(item) && canRun(item)) ?? null);
  const first = engine?.connections.find(working);
  const more = engine ? engine.connections.filter(working).length - 1 : 0;
  const month = summary?.month;
  const share =
    month && month.cap !== null && month.cap > 0
      ? Math.min(1, month.spent / month.cap)
      : null;

  return (
    <div className={styles.band} role="group" aria-label="Translation status">
      <div className={styles.fact}>
        {status ? (
          <StatusBadge tone={status.tone} label={status.label} />
        ) : (
          <StatusBadge tone="neutral" label="Off" />
        )}
        <div className={styles.factMain}>
          {engine ? (
            <>
              <span className={styles.factStrong}>{engineName(engine)}</span>
              {status?.next ? (
                <span>{status.next}</span>
              ) : (
                hasBackends(engine) &&
                first && (
                  <span>
                    {backendName(engine, first)}
                    {more > 0 ? `, then ${more} more` : ""}
                  </span>
                )
              )}
            </>
          ) : (
            <>
              <span className={styles.factStrong}>Translation is off</span>
              <span>
                {waiting
                  ? `${engineName(waiting)} was turned off and needs a look. Open its set-up to turn it on again.`
                  : "Pick an engine to turn it on."}
              </span>
            </>
          )}
        </div>
      </div>
      {summary && (
        <div className={styles.fact}>
          <div className={styles.factMain}>
            <span className={styles.factLabel}>Queue</span>
            <span className={styles.factStrong}>
              {summary.queue.running} running
            </span>
            <span>{summary.queue.pending} waiting</span>
          </div>
        </div>
      )}
      {month && (
        <div className={styles.fact}>
          <div className={styles.spend}>
            <div className={styles.factMain}>
              <span className={styles.factLabel}>This month</span>
              <span className={styles.factStrong}>{money(month.spent)}</span>
              <span>
                {month.cap === null ? "no cap" : `of ${money(month.cap)}`}
              </span>
            </div>
            {share !== null && (
              <div
                className={styles.meter}
                role="meter"
                aria-label="Spend this month against the cap"
                aria-valuemin={0}
                aria-valuemax={month.cap ?? 0}
                aria-valuenow={Math.min(month.spent, month.cap ?? 0)}
                aria-valuetext={`${money(month.spent)} of ${money(month.cap ?? 0)}`}
              >
                <div
                  className={styles.meterFill}
                  data-tone={spendTone(month.spent, month.cap)}
                  style={{ width: `${Math.max(share * 100, 1.5)}%` }}
                />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default StatusBand;
