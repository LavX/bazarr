/* eslint-disable camelcase -- transport field names. */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Button, Group, Text } from "@mantine/core";
import { faPaperPlane } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSeerrMedia, useSeerrRequestMutation } from "@/apis/hooks/seerr";
import { useSystemSettings } from "@/apis/hooks/system";
import type { MetadataTitle } from "@/types/discover";
import type {
  SeerrIdentity,
  SeerrMediaResponse,
  SeerrMediaState,
  SeerrRequestBody,
  SeerrRequestOutcome,
} from "@/types/seerr";
import SeerrRequestModal from "./SeerrRequestModal";
import { groupSeerrSeasons } from "./seerrSeasons";
import styles from "./Discover.module.scss";

const REJECTED_KEY_FLAG = "bazarr.seerr.rejected-key-shown";

export function seerrIdentity(
  title: MetadataTitle | null,
): SeerrIdentity | null {
  if (!title) return null;
  if (title.source === "tmdb" && typeof title.id === "number") {
    return title.media_type === "show"
      ? { kind: "tv", tmdbId: title.id }
      : { kind: "movie", tmdbId: title.id };
  }
  if (
    title.source === "local" &&
    title.media_type === "show" &&
    typeof title.tvdb_id === "number"
  ) {
    return { kind: "tv", tvdbId: title.tvdb_id };
  }
  if (
    title.source === "local" &&
    title.media_type === "movie" &&
    typeof title.tmdb_id === "number" &&
    title.tmdb_id > 0
  ) {
    return { kind: "movie", tmdbId: title.tmdb_id };
  }
  return null;
}

const BADGES: Record<string, string> = {
  pending: "Awaiting approval",
  processing: "Processing",
  declined: "Declined",
  failed: "Failed",
  partially_available: "Some seasons available",
  available: "Available in Seerr",
  blocklisted: "Blocklisted",
};

// Order matters, and APPROVED is the whole reason. Seerr leaves a finished
// request at APPROVED rather than tidying it away, so reading that before the
// media row reported "Processing" over a title already available, and hid
// "Some seasons available" (with its seasons flow) behind the stale row. Only
// that one check moves below the media row. A request still pending, declined
// or failed stays above it: a series Seerr holds in full can have a brand new
// season requested, and the badge is the only place the reader learns what
// became of it.
function badgeFor(state: SeerrMediaState): string | null {
  if (state.request?.status === "pending") return BADGES.pending;
  if (state.request?.status === "declined") return BADGES.declined;
  if (state.request?.status === "failed") return BADGES.failed;
  if (state.status === "available") return BADGES.available;
  if (state.status === "blocklisted") return BADGES.blocklisted;
  if (state.status === "partially_available") return BADGES.partially_available;
  if (state.status === "processing" || state.request?.status === "approved")
    return BADGES.processing;
  if (state.status === "pending") return BADGES.pending;
  // Only "unknown" and "deleted" reach here, and neither carries a badge.
  return null;
}

// Identifies the title a notice or a pending submit belongs to. Separate from
// the react-query key on purpose: this only has to tell one selected title
// from the next within a single mounted SeerrAction.
function identityKey(identity: SeerrIdentity): string {
  return "tmdbId" in identity
    ? `${identity.kind}:tmdb:${identity.tmdbId}`
    : `${identity.kind}:tvdb:${identity.tvdbId}`;
}

// A 400 is Bazarr refusing its own payload before anything was sent, which
// "Seerr is unreachable." would misreport as the server's fault.
function errorText(error: unknown): string {
  const status = (error as { response?: { status?: number } } | null)?.response
    ?.status;
  return status === 400
    ? "Bazarr could not send that request."
    : "Seerr is unreachable.";
}

function outcomeText(outcome: SeerrRequestOutcome): string {
  if ("outcome" in outcome) {
    return outcome.outcome === "requested"
      ? "Requested in Seerr."
      : outcome.outcome === "already_requested"
        ? "Already requested in Seerr."
        : "Nothing left to request.";
  }
  return {
    permission: "Seerr refused the request: the owner lacks permission.",
    quota: "Seerr refused the request: quota exceeded.",
    blocklisted: "This title is blocklisted in Seerr.",
    csrf_blocked:
      "Seerr blocked the request. Disable CSRF protection in Seerr's network settings.",
    validation: "Seerr rejected the request payload.",
    upstream_error: "Seerr could not complete the request.",
    rejected_key: "Seerr rejected the API key.",
    unreachable: "Seerr is unreachable.",
    not_configured: "Seerr is not configured.",
  }[outcome.error_code];
}

export default function SeerrAction({
  title,
  inLibrary,
  ownershipUncertain = false,
}: {
  title: MetadataTitle | null;
  inLibrary: boolean;
  /**
   * The library read was truncated, partly unattributed, or failed, so what
   * the page knows about local copies and owned seasons is a floor. Distinct
   * from the hero's own "might this be in the library after all", which only
   * matters for a title believed absent: the season grouping needs the
   * opposite case, a title that is present but incompletely read.
   */
  ownershipUncertain?: boolean;
}) {
  const { data: settings } = useSystemSettings();
  const enabled = settings?.general?.use_seerr === true;
  const identity = useMemo(
    () => (enabled ? seerrIdentity(title) : null),
    [enabled, title],
  );
  const query = useSeerrMedia(identity, enabled && identity !== null);
  const mutation = useSeerrRequestMutation();
  const [modalOpen, setModalOpen] = useState(false);
  // The notice carries the title it belongs to. This component is not keyed
  // to the selection, so without that a notice from one title outlived it and
  // read as the next title's result.
  const [message, setMessage] = useState<{
    key: string;
    text: string;
    isError: boolean;
  } | null>(null);
  const currentKey = identity ? identityKey(identity) : null;
  // Same reason, for the in-flight submit: the request's own variables say
  // which title it belongs to, so a request still running does not light up
  // the button of whichever title the reader moved on to.
  const pendingKey = mutation.variables
    ? identityKey(mutation.variables.identity)
    : null;

  const data: SeerrMediaResponse | undefined = query.data;
  const isRejectedKeyError =
    !!data &&
    "configured" in data &&
    "error_code" in data &&
    data.error_code === "rejected_key";

  // Captured once at mount, before the effect below ever writes: whether a
  // previous SeerrAction instance already showed this banner earlier in the
  // session. This value must never change after mount, or the banner would
  // hide itself the instant its own write below lands. Reading and writing
  // sessionStorage directly in the render body doesn't work here: StrictMode
  // double-invokes render in development, so the discarded first pass's
  // write was already visible to the second, kept pass, and the banner could
  // fail to render on a genuine first mount.
  const [rejectedKeyAlreadyShown] = useState(() => {
    try {
      return sessionStorage.getItem(REJECTED_KEY_FLAG) === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    if (!isRejectedKeyError || rejectedKeyAlreadyShown) return;
    try {
      sessionStorage.setItem(REJECTED_KEY_FLAG, "1");
    } catch {
      /* storage unavailable */
    }
  }, [isRejectedKeyError, rejectedKeyAlreadyShown]);

  const submit = useCallback(
    (body: SeerrRequestBody) => {
      if (!identity || !currentKey) return;
      const key = currentKey;
      setMessage(null);
      mutation.mutate(
        { body, identity },
        {
          onSuccess: (outcome) =>
            setMessage({
              key,
              text: outcomeText(outcome),
              isError: "error_code" in outcome,
            }),
          onError: (error) =>
            setMessage({ key, text: errorText(error), isError: true }),
        },
      );
    },
    [mutation, identity, currentKey],
  );

  if (!enabled || identity === null) return null;
  if (query.isPending) {
    return (
      <span role="status" className={styles.seerrStatus}>
        Checking Seerr…
      </span>
    );
  }
  if (!data || !("configured" in data)) return null;
  if ("error_code" in data) {
    if (data.error_code === "unreachable") {
      return (
        <Group gap="xs" className={styles.seerrStatus}>
          <Text size="sm">Seerr is unreachable</Text>
          <Button
            size="compact-sm"
            variant="subtle"
            onClick={() => void query.refetch()}
          >
            Retry
          </Button>
        </Group>
      );
    }
    if (data.error_code === "rejected_key") {
      if (rejectedKeyAlreadyShown) return null;
      return (
        <Text size="sm" className={styles.seerrStatus}>
          Seerr rejected the API key.{" "}
          <Anchor component={Link} to="/settings/connections#seerr">
            Check it in settings
          </Anchor>
          .
        </Text>
      );
    }
    return null;
  }

  const state = data;
  const isShow = identity.kind === "tv";
  const tmdbId = "tmdbId" in identity ? identity.tmdbId : state.tmdb_id;
  const badge = badgeFor(state);
  // A completed show is still `requestable` (TMDB can know a season Seerr
  // does not), but if the same season-grouping arithmetic the modal uses
  // says nothing is actually selectable, offering the button just opens a
  // modal with a disabled submit. Suppress it then, unless the 4K lane is
  // still open: that lane always has "Request all seasons" to offer.
  const seasonGroups = isShow ? groupSeerrSeasons(title!, state) : null;
  const seasonsExhausted = seasonGroups?.exhausted === true;
  const canRequest =
    (state.requestable || state.requestable_4k) &&
    tmdbId !== undefined &&
    !(inLibrary && !isShow && !state.known) &&
    !(seasonsExhausted && !state.requestable_4k);
  const needsModal = isShow || state.requestable_4k;
  const requestLabel =
    state.status === "partially_available"
      ? "Request seasons"
      : "Request in Seerr";

  const onRequest = () => {
    if (needsModal) {
      setModalOpen(true);
      return;
    }
    submit({ media_type: "movie", tmdb_id: tmdbId!, is4k: false });
  };

  return (
    <div className={styles.seerrAction}>
      {badge && <Badge variant="light">{badge}</Badge>}
      {canRequest && (
        <Button
          size="compact-sm"
          // Not filled: "Open in library" sits in this same row, and the
          // stylesheet spends the amber fill on one action per row.
          variant="default"
          leftSection={<FontAwesomeIcon icon={faPaperPlane} />}
          loading={mutation.isPending && pendingKey === currentKey}
          onClick={onRequest}
        >
          {requestLabel}
        </Button>
      )}
      {state.link && state.known && (
        <Anchor
          href={state.link}
          target="_blank"
          rel="noopener noreferrer"
          size="sm"
        >
          Open in Seerr
        </Anchor>
      )}
      {canRequest && (
        <Text size="xs" c="dimmed">
          Requested as the Seerr owner and approved immediately.
          {ownershipUncertain &&
            " Your library check is incomplete, so what you already own may not be fully reflected here."}
        </Text>
      )}
      {message && message.key === currentKey && (
        <Text size="sm" role={message.isError ? "alert" : "status"}>
          {message.text}
        </Text>
      )}
      {modalOpen && tmdbId !== undefined && (
        <SeerrRequestModal
          title={title!}
          state={state}
          tmdbId={tmdbId}
          ownershipUncertain={ownershipUncertain}
          onClose={() => setModalOpen(false)}
          onSubmit={(body) => {
            setModalOpen(false);
            submit(body);
          }}
        />
      )}
    </div>
  );
}
