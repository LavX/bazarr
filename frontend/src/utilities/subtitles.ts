const syncEngineLabels: Record<string, string> = {
  "sync-ffsubsync": "FFsubsync",
  "sync-autosubsync": "Autosubsync",
  "sync-alass": "ALASS",
};

const syncEngineOrder = ["sync-ffsubsync", "sync-autosubsync", "sync-alass"];
const syncOutputFilenameMarkers = [".ffsubsync.", ".autosubsync.", ".alass."];
const syncOutputLanguageModifiers = Object.keys(syncEngineLabels);

export type SubtitleSyncStatus = {
  synced: boolean;
  confirmed: boolean;
  editedAfterSync: boolean;
  lastModified: number;
  lastSyncTimestamp: string | null;
  jobStatus?: "pending" | "running" | null;
  jobId?: number | null;
};

export type SubtitleSyncStatusPresentation = {
  icon: "sync" | "question" | "running";
  label: string;
};

export function buildSubtitleLanguageKey(subtitle: Subtitle): string {
  if (subtitle.language) {
    return subtitle.language;
  }

  let key = subtitle.code2;
  if (subtitle.hi) key += ":hi";
  if (subtitle.forced) key += ":forced";
  if (subtitle.modifier) key += `:${subtitle.modifier}`;
  return key;
}

export function isSyncOutputSubtitle(subtitle: Subtitle): boolean {
  if (isSyncOutputLanguageKey(subtitle.language)) {
    return true;
  }

  if (subtitle.modifier?.startsWith("sync-") === true) {
    return true;
  }

  const path = subtitle.path?.toLowerCase() ?? "";
  return syncOutputFilenameMarkers.some((marker) => path.includes(marker));
}

export function isSyncOutputLanguageKey(
  language: string | null | undefined,
): boolean {
  const modifier = language?.split(":", 2)[1]?.toLowerCase();
  return syncOutputLanguageModifiers.includes(modifier ?? "");
}

export function canSynchronizeSubtitle(subtitle: Subtitle): boolean {
  return !isSyncOutputSubtitle(subtitle);
}

export function getSyncEngineLabel(modifier: string | null | undefined) {
  if (!modifier) {
    return "Original";
  }
  return syncEngineLabels[modifier] ?? modifier.replace(/^sync-/, "");
}

export function sortSyncOutputSubtitles(subtitles: Subtitle[]) {
  return [...subtitles].sort((left, right) => {
    const leftIndex = syncEngineOrder.indexOf(left.modifier ?? "");
    const rightIndex = syncEngineOrder.indexOf(right.modifier ?? "");
    return (
      (leftIndex === -1 ? 99 : leftIndex) -
      (rightIndex === -1 ? 99 : rightIndex)
    );
  });
}

export function getSubtitleSyncStatusPresentation(
  status: SubtitleSyncStatus | null | undefined,
): SubtitleSyncStatusPresentation | null {
  if (status?.jobStatus === "running") {
    return {
      icon: "running",
      label: "Sync running",
    };
  }

  if (status?.jobStatus === "pending") {
    return {
      icon: "running",
      label: "Sync queued",
    };
  }

  if (!status?.synced) {
    return null;
  }

  if (!status?.confirmed) {
    return {
      icon: "question",
      label: "Sync unconfirmed",
    };
  }

  return {
    icon: "sync",
    label: "Sync",
  };
}
