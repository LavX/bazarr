/**
 * The rule the Connections instance modal applies, worded the same way, so a
 * URL the wizard accepts is one Settings accepts too. The backend enforces it
 * again (media_servers/http.py::validate_server_url).
 */
export function validateServerUrl(value: string): string | null {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) &&
      !parsed.username &&
      !parsed.password &&
      !parsed.search &&
      !parsed.hash
      ? null
      : "Enter a full HTTP or HTTPS URL without credentials, query or fragment";
  } catch {
    return "Enter a full HTTP or HTTPS URL";
  }
}
