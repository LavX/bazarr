let counter = 0;

// crypto.randomUUID() is only defined in secure contexts (HTTPS, localhost,
// 127.0.0.1). On plain-http LAN origins like http://192.168.x.x it is
// undefined and calling it throws TypeError, which breaks the subtitle parsers.
export function cueId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function" &&
    (window.isSecureContext ||
      location.hostname === "localhost" ||
      location.hostname === "127.0.0.1")
  ) {
    return crypto.randomUUID();
  }
  return `cue-${Date.now()}-${counter++}`;
}
