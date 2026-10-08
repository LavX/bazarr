import { showNotification } from "@mantine/notifications";
import Axios, { AxiosError, AxiosInstance, CancelTokenSource } from "axios";
import type { SilentRequestConfig } from "@/apis/raw/base";
import { notification } from "@/modules/notification";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import socketio from "@/modules/socketio";
import { Environment } from "@/utilities";
import { LOG } from "@/utilities/console";
import { setAuthenticated } from "@/utilities/event";
import { inlineErrorsRequested } from "./inlineWrites";

declare module "axios" {
  interface AxiosRequestConfig {
    // Set by withInlineErrors: the caller shows this request's failure itself.
    inlineErrors?: boolean;
  }
}

function GetErrorMessage(data: unknown, defaultMsg = "Unknown error"): string {
  if (typeof data === "string") {
    return data;
  } else if (typeof data === "object" && data !== null) {
    const obj = data as Record<string, unknown>;
    if (typeof obj.error === "string") return obj.error;
    if (typeof obj.message === "string") return obj.message;
  }
  return defaultMsg;
}

class BazarrClient {
  axios!: AxiosInstance;
  source!: CancelTokenSource;
  bIsAuthenticated: boolean;

  constructor() {
    this.bIsAuthenticated = false;
    const baseUrl = `${Environment.baseUrl}/api/`;

    this.initialize(baseUrl, Environment.apiKey);
    socketio.initialize();
  }

  initialize(url: string, apikey?: string) {
    LOG("info", "initializing BazarrClient with baseUrl", url);

    this.axios = Axios.create({
      baseURL: url,
    });

    this.axios.defaults.headers.post["Content-Type"] = "application/json";
    this.axios.defaults.headers.common["X-API-KEY"] = apikey ?? "AUTH_NEEDED";

    this.source = Axios.CancelToken.source();

    // Synchronous, so it runs inside the call that sends the request, which
    // is where withInlineErrors can see it. An asynchronous interceptor anywhere
    // in the chain would make every one of them run later.
    this.axios.interceptors.request.use(
      (config) => {
        config.cancelToken = this.source.token;
        if (inlineErrorsRequested()) {
          config.inlineErrors = true;
        }
        return config;
      },
      undefined,
      { synchronous: true },
    );

    this.axios.interceptors.response.use(
      (resp) => {
        if (resp.status >= 200 && resp.status < 300) {
          if (!this.bIsAuthenticated) {
            this.bIsAuthenticated = true;
            setAuthenticated(true);
          }
          return Promise.resolve(resp);
        } else {
          const error: BackendError = {
            code: resp.status,
            message: GetErrorMessage(resp.data),
          };
          this.handleError(error, resp.config?.inlineErrors === true);
          return Promise.reject(resp);
        }
      },
      (error: AxiosError) => {
        // A confirmation refusal voids the remembered window, so the next
        // gated action prompts again instead of believing it is confirmed
        // (after a restart, or when the server's window is shorter).
        if (
          error.response?.status === 403 &&
          (error.response?.data as { code?: string } | undefined)?.code ===
            "confirm_password"
        ) {
          forgetPasswordConfirmation();
        }
        // A blob response (file downloads) carries its error body as a Blob,
        // which cannot be read synchronously: give it a status-based message
        // (never the disconnect sentinel, which would suppress the 401
        // handling below) and let the caller surface the real reason.
        const data = error.response?.data;
        const isBlobBody = typeof Blob !== "undefined" && data instanceof Blob;
        const message = isBlobBody
          ? `Error ${error.response?.status ?? 500}`
          : GetErrorMessage(data, "You have disconnected from the server");

        const backendError: BackendError = {
          code: error.response?.status ?? 500,
          message,
        };

        error.message = backendError.message;
        // A request flagged silentError owns its own user-facing failure for
        // these two statuses (the password confirm modal answers 403 and 429
        // inline), so the shared handler stays quiet rather than toasting on
        // top of the modal's message. Any other status still toasts.
        const silentByRequest =
          (error.config as SilentRequestConfig | undefined)?.silentError ===
            true &&
          (backendError.code === 403 || backendError.code === 429);
        this.handleError(
          backendError,
          isBlobBody ||
            error.config?.inlineErrors === true ||
            silentByRequest,
        );

        return Promise.reject(error);
      },
    );
  }

  handleError(error: BackendError, silent = false) {
    const { code, message } = error;

    // Backend not ready yet: suppress everything, don't trigger auth changes
    if (
      code === 0 ||
      code === 502 ||
      code === 503 ||
      message === "You have disconnected from the server" ||
      message === "Network Error" ||
      message === "Backend is starting up"
    ) {
      LOG("warning", "Backend unreachable, suppressing error");
      return;
    }

    switch (code) {
      case 401:
        this.bIsAuthenticated = false;
        setAuthenticated(false);
        return;
      case 409:
      case 412:
        // Skip notification, let the caller handle via onError / .catch()
        return;
    }

    LOG("error", "A error has occurred", code);
    if (silent) {
      // The caller owns the user-facing message (e.g. blob downloads read
      // the error body asynchronously); avoid a duplicate generic toast.
      return;
    }
    showNotification(notification.error(`Error ${code}`, message));
  }
}

const client = new BazarrClient();
export default client;
