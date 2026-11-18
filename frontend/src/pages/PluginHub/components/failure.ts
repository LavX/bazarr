/** What went wrong, and what to do next, in a sentence each. Never a command line. */
export interface FailureCopy {
  cause: string;
  next: string;
}

interface Rule {
  test: RegExp;
  copy: (message: string) => FailureCopy;
}

// The first rule that matches wins, so the narrow causes come before the broad ones.
const RULES: Rule[] = [
  {
    test: /\bproxy\b|\b407\b|tunnel/i,
    copy: () => ({
      cause: "Your proxy blocked the download.",
      next: "Check the proxy setting in Settings > General.",
    }),
  },
  {
    test: /release\b.*\brevoked|revoked\b.*\brelease/i,
    copy: () => ({
      cause: "The publisher revoked this release.",
      next: "Install a newer release when the catalog lists one.",
    }),
  },
  {
    test: /expired|clock (looks )?wrong|host's clock/i,
    copy: () => ({
      cause: "The signed catalog has expired, or this host's clock is wrong.",
      next: "Check that this host's clock is right, then refresh the catalog.",
    }),
  },
  {
    test: /signature|signed with a key|signed catalog|revoked key|replayed|catalog.*malformed/i,
    copy: () => ({
      cause: "The catalog's signature did not check out.",
      next: "Refresh the catalog. If this keeps happening, do not install from it.",
    }),
  },
  {
    test: /rate.?limit|could not be asked|could not be reached|time budget|name resolution|temporary failure/i,
    copy: () => ({
      cause: "The catalog or the download could not be reached.",
      next: "Wait a few minutes and try again.",
    }),
  },
  {
    test: /\bneeds\b.+\bwhich\b/i,
    // The server names the requirement in words of its own, so they are kept.
    copy: (message) => ({
      cause: message.trim(),
      next: "Install the plugin that provides it first, or choose another release.",
    }),
  },
  {
    test: /wheel|no matching distribution|platform|architecture|abi\b/i,
    copy: () => ({
      cause: "A package this plugin needs is not available for this platform.",
      next: "Ask the plugin's author for a build for this platform.",
    }),
  },
  {
    test: /landlock|docker|container/i,
    copy: () => ({
      cause:
        "This host cannot give the plugin the sandbox or Docker access it needs.",
      next: "Check the notes on the plugin's card, or leave it switched off.",
    }),
  },
  {
    test: /environment|venv|virtualenv|\bpip\b|dependenc|rebuild/i,
    copy: () => ({
      cause: "The plugin's environment could not be built.",
      next: "Use Repair to rebuild it. If it fails again, open the log.",
    }),
  },
];

/**
 * The fixed sentences for a cause the server reports. A message that matches none of them comes back
 * as the plugin's own scrubbed words with a pointer to the log, and a missing one as nothing at all.
 */
export function explainFailure(
  message: string | null | undefined,
): FailureCopy | null {
  const text = (message ?? "").trim();
  if (!text) {
    return null;
  }
  const rule = RULES.find((item) => item.test.test(text));
  return rule
    ? rule.copy(text)
    : { cause: text, next: "Open the log for more detail." };
}
