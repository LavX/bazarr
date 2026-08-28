/**
 * Extract the filename a server suggested via Content-Disposition.
 *
 * Handles both the RFC 5987 `filename*=UTF-8''...` form (which Flask's
 * send_file emits for non-ASCII names) and the plain quoted `filename="..."`
 * form. Returns the fallback when the header is absent or unparsable.
 */
export function filenameFromContentDisposition(
  header: string | undefined | null,
  fallback: string,
): string {
  if (!header) {
    return fallback;
  }

  const extended = header.match(/filename\*=(?:UTF-8|utf-8)''([^;]+)/);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      // Malformed percent-encoding; fall through to the plain form.
    }
  }

  const plain = header.match(/filename="?([^";]+)"?/);
  if (plain) {
    return plain[1].trim();
  }

  return fallback;
}

/**
 * Hand a blob to the browser as a file download.
 */
export function saveBlobAs(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
