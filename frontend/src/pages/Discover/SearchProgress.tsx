import type { DiscoverSearchProgress } from "@/types/discover";
import styles from "./Discover.module.scss";

export default function SearchProgress({
  progress,
}: {
  progress?: DiscoverSearchProgress;
}) {
  const providers = progress?.providers ?? [];
  const pending = providers.filter((provider) => provider.status === "pending");
  const eligible = providers.filter(
    (provider) => provider.status !== "skipped",
  );
  const done = eligible.length - pending.length;
  const found = providers.reduce(
    (sum, provider) => sum + provider.result_count,
    0,
  );
  const skipped = providers.filter(
    (provider) => provider.status === "skipped",
  ).length;
  return (
    <div className={styles.searchProgress}>
      <div className={styles.searchProgressHeading}>
        <strong>
          {!providers.length
            ? "Preparing parallel search…"
            : pending.length
              ? "Searching providers in parallel"
              : "Preparing subtitle results…"}
        </strong>
        {providers.length > 0 && (
          <span>
            {pending.length} pending · {done} finished · {found}{" "}
            {found === 1 ? "result" : "results"}
            {skipped ? ` · ${skipped} not applicable` : ""}
          </span>
        )}
      </div>
      <progress
        aria-label="Providers checked"
        max={eligible.length || 1}
        value={eligible.length ? done : undefined}
      />
      {pending.length > 0 && (
        <p>
          Awaiting results:{" "}
          {pending.map((provider) => provider.provider).join(", ")}
        </p>
      )}
    </div>
  );
}
