import { isAxiosError } from "axios";

/** The longest text body that is taken for a sentence the server wrote for a person. */
const SENTENCE_LIMIT = 300;

/**
 * The server's own words in a refusal: the message of a body that is an object, or a body that is
 * plain text. An error page from a proxy is text too, so a body that is markup, or too long to be a
 * sentence, is not shown: the dialog says what failed in its own words instead.
 */
export function refusalText(body: unknown): string {
  if (typeof body === "string") {
    const text = body.trim();
    return text.startsWith("<") || text.length >= SENTENCE_LIMIT ? "" : text;
  }
  if (typeof body === "object" && body !== null) {
    const message = (body as { message?: unknown }).message;
    return typeof message === "string" ? message.trim() : "";
  }
  return "";
}

/** Whether the server refused because it wants the password confirmed first. */
export const needsPassword = (error: unknown) =>
  isAxiosError(error) &&
  error.response?.status === 403 &&
  (error.response.data as { code?: string } | undefined)?.code ===
    "confirm_password";

/** Whether the server refused because what the person agreed to is no longer what the install would add. */
export const consentChanged = (error: unknown) =>
  isAxiosError(error) && error.response?.status === 409;

/**
 * Why a request or a job did not go through, in a sentence for a person: the server's words when it
 * gave any, or the failed job's own reason. An empty string when there is nothing to say, and the
 * dialog then uses its own sentence. Never the transport's text.
 */
export function reasonOf(error: unknown): string {
  if (isAxiosError(error)) {
    return refusalText(error.response?.data);
  }
  return error instanceof Error ? error.message.trim() : "";
}
