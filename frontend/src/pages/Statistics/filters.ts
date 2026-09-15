/**
 * Filter state shared by every metrics tab.
 *
 * Lifted to the page so switching tabs keeps the same window and filters:
 * narrowing to one provider on the leaderboard and then opening Quality should
 * show that provider's quality, not reset to everything.
 */
export interface StatisticsFilters {
  timeFrame: History.TimeFrameOptions;
  action: Nullable<History.ActionOptions>;
  provider: Nullable<System.Provider>;
  language: Nullable<Language.Server>;
}

export const defaultStatisticsFilters: StatisticsFilters = {
  timeFrame: "month",
  action: null,
  provider: null,
  language: null,
};

/** Human label for a history action id. The ids only exist in the frontend. */
export const ACTION_LABELS: Record<number, string> = {
  0: "Deleted",
  1: "Automatic",
  2: "Manual",
  3: "Upgraded",
  4: "Uploaded",
  5: "Synced",
  6: "Translated",
  7: "Embedded",
};

export const actionLabel = (action: number) =>
  ACTION_LABELS[action] ?? `Action ${action}`;
