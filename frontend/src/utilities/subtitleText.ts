// Convert subtitle text with tags into safe display HTML
export function renderSubtitleHtml(text: string): string {
  // Strip ASS override tags like {\i1}, {\b1}, {\an8}, {\pos(x,y)}, etc.
  let html = text.replace(/\{\\[^}]*\}/g, "");
  // Allow basic HTML formatting tags, escape everything else
  // First escape HTML entities
  html = html
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  // Restore safe tags
  html = html
    .replace(/&lt;i&gt;/gi, "<i>")
    .replace(/&lt;\/i&gt;/gi, "</i>")
    .replace(/&lt;b&gt;/gi, "<b>")
    .replace(/&lt;\/b&gt;/gi, "</b>")
    .replace(/&lt;u&gt;/gi, "<u>")
    .replace(/&lt;\/u&gt;/gi, "</u>")
    .replace(/&lt;s&gt;/gi, "<s>")
    .replace(/&lt;\/s&gt;/gi, "</s>");
  // Strip font tags (render as plain text)
  html = html
    .replace(/&lt;font[^&]*&gt;/gi, "")
    .replace(/&lt;\/font&gt;/gi, "");
  // Convert newlines to <br>
  html = html.replace(/\n/g, "<br>");
  return html;
}
