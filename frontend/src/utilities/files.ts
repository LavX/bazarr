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
      const decoded = decodeURIComponent(extended[1].trim());
      if (decoded) {
        return decoded;
      }
    } catch {
      // Malformed percent-encoding; fall through to the plain forms.
    }
  }

  // Quoted form first, consumed to the closing quote: a semicolon INSIDE the
  // quotes is part of the filename, not a parameter separator, and escaped
  // quotes/backslashes (quoted-pairs) are part of the value.
  const quoted = header.match(/filename="((?:[^"\\]|\\.)*)"/);
  if (quoted) {
    const unescaped = quoted[1].replace(/\\(.)/g, "$1").trim();
    if (unescaped) {
      return unescaped;
    }
  }

  const token = header.match(/filename=([^;"\s]+)/);
  if (token) {
    return token[1].trim();
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
