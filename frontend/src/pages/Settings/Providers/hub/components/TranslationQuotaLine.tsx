import { FunctionComponent } from "react";
import { Text, Tooltip } from "@mantine/core";
import type { ProviderHubRuntimeStatus } from "@/apis/raw/providerHub";

function resetLabel(value: string | null): string {
  if (!value) return "";
  const isoDay = /^\d{4}-\d{2}-\d{2}/.exec(value)?.[0];
  if (isoDay) {
    const date = new Date(isoDay + "T12:00:00Z");
    if (!Number.isNaN(date.getTime())) {
      const month = new Intl.DateTimeFormat("en", {
        month: "short",
        timeZone: "UTC",
      }).format(date);
      return `${date.getUTCDate()} ${month}`;
    }
  }
  return value;
}

function quotaText(status: ProviderHubRuntimeStatus): string | null {
  const reset = resetLabel(status.reset_at);
  const suffix = reset ? `, resets ${reset}` : "";
  if (status.entitled === false) {
    return "AI translation is not included in this account";
  }
  if (status.exhausted === true) {
    return `AI translation quota used up${suffix}`;
  }
  if (status.remaining !== null) {
    return status.limit !== null
      ? `AI translation: ${status.remaining} of ${status.limit} left${suffix}`
      : `AI translation: ${status.remaining} left${suffix}`;
  }
  if (status.entitled === true && status.exhausted === false) {
    return "AI translation available";
  }
  return null;
}

export const TranslationQuotaLine: FunctionComponent<{
  status?: ProviderHubRuntimeStatus;
}> = ({ status }) => {
  if (!status) return null;
  const text = quotaText(status);
  if (!text) return null;
  return (
    <Tooltip
      label={`Reported ${new Date(status.reported_at).toLocaleString()}`}
    >
      <Text size="xs" c="dimmed">
        {text}
      </Text>
    </Tooltip>
  );
};
