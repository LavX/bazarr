import { Link } from "react-router";
import {
  faArrowRight,
  faCircleExclamation,
  faTriangleExclamation,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useDiscoverSummary } from "@/apis/hooks/discover";
import styles from "./Discover.module.scss";

/**
 * The conditions that quietly stop a search from finding anything.
 *
 * A provider cooling down, a disconnected library sync and an unreachable root
 * folder all present as "nothing was found", which is indistinguishable from
 * there being nothing to find. The summary has carried these for a while with
 * nothing rendering them, so a reader could only discover them by reasoning
 * about an absence.
 *
 * It says nothing when there is nothing to say: a healthy install should not be
 * given a panel reporting its own health, or the panel stops being read.
 */
export default function NeedsAttention() {
  const summary = useDiscoverSummary();
  const attention = summary.data?.attention;
  const items = attention?.items ?? [];
  // A source that could not be read is its own kind of finding: the honest
  // answer is that this is not the whole picture, not that all is well.
  const unreadable =
    attention !== undefined &&
    (attention.availability !== "available" ||
      attention.unknown_sources.length > 0);
  if (!items.length && !unreadable) return null;
  return (
    <section
      className={styles.attention}
      aria-labelledby="discover-attention-title"
    >
      <h2 id="discover-attention-title">
        <FontAwesomeIcon icon={faTriangleExclamation} aria-hidden="true" />{" "}
        Needs attention
      </h2>
      <ul>
        {items.map((item) => (
          <li key={item.id} data-severity={item.severity}>
            <FontAwesomeIcon
              icon={
                item.severity === "error"
                  ? faCircleExclamation
                  : faTriangleExclamation
              }
              aria-hidden="true"
            />
            <span className={styles.attentionCopy}>
              <strong>{item.summary}</strong>
              {item.detail && <span>{item.detail}</span>}
            </span>
            <Link to={item.recovery.target}>
              {item.recovery.label} <FontAwesomeIcon icon={faArrowRight} />
            </Link>
          </li>
        ))}
        {unreadable && (
          <li data-severity="warning">
            <FontAwesomeIcon icon={faTriangleExclamation} aria-hidden="true" />
            <span className={styles.attentionCopy}>
              <strong>
                {items.length
                  ? "There may be more than this."
                  : "This check could not be completed."}
              </strong>
              <span>Some of what this reports on could not be read.</span>
            </span>
          </li>
        )}
      </ul>
    </section>
  );
}
