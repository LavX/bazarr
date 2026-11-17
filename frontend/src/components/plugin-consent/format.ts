const UNITS = ["B", "KB", "MB", "GB"];

/** A size in bytes in the unit a person reads, "12.4 MB". */
export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text =
    unit === 0 || value >= 100
      ? Math.round(value).toString()
      : value.toFixed(1);
  return `${text.replace(/\.0$/, "")} ${UNITS[unit]}`;
}

/** A dollar amount with its cents, "$12.50". */
export function formatDollars(amount: number): string {
  return `$${amount.toFixed(2)}`;
}

const PERMISSION_LABELS: Record<string, string> = {
  network: "Network access",
  media_read: "Read your media folders",
  data_dir: "Keep its own data folder",
  binaries: "Run programs it ships",
  docker: "Docker access",
  interactive_login: "Sign in interactively",
};

/** What a permission means in words. A permission this page does not know is shown as the host named it. */
export function permissionLabel(name: string): string {
  return PERMISSION_LABELS[name] ?? name;
}

const KIND_LABELS: Record<string, string> = {
  translator: "Translator",
  translator_backend: "Translator back end",
  service: "Service",
  subtitle_provider: "Subtitle provider",
};

/** What a kind of plugin is called. */
export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind.replace(/_/g, " ");
}
