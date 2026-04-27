import BaseApi from "./base";

interface JellyfinTestResult {
  success: boolean;
  server_name?: string;
  version?: string;
  // Coarse error classification only; raw exception text never crosses the
  // wire (see bazarr/jellyfin/operations.py::jellyfin_test_connection).
  error_code?: "configuration" | "connection_failed";
}

interface JellyfinRefreshResult {
  success: boolean;
  movies_total: number;
  movies_refreshed: number;
  series_total: number;
  series_refreshed: number;
  error_code?:
    | "configuration"
    | "connection_failed"
    | "no_libraries_configured";
}

interface JellyfinLibrary {
  id: string;
  name: string;
  type: string;
}

class JellyfinApi extends BaseApi {
  constructor() {
    super("/jellyfin");
  }

  async testConnection(url: string, apikey: string, verifySsl?: boolean) {
    const body: Record<string, string> = { url, apikey };
    if (verifySsl !== undefined) body.verify_ssl = verifySsl ? "true" : "false";
    const response = await this.post<JellyfinTestResult>(
      "/test-connection",
      body,
    );

    return response.data;
  }

  async libraries(url?: string, apikey?: string, verifySsl?: boolean) {
    // POST so apikey rides in the request body. apikey-in-URL leaks into
    // browser history, reverse-proxy access logs, and any URL telemetry.
    const body: Record<string, string> = {};
    if (url) body.url = url;
    if (apikey) body.apikey = apikey;
    if (verifySsl !== undefined) body.verify_ssl = verifySsl ? "true" : "false";

    // post() returns AxiosResponse<T> (not unwrapped like get()), so peel
    // both the AxiosResponse envelope and the Bazarr `{data: [...]}` wrap.
    const response = await this.post<{ data: JellyfinLibrary[] }>(
      "/libraries",
      body,
    );

    return response.data.data;
  }

  async refreshLibraries() {
    const response = await this.post<JellyfinRefreshResult>(
      "/refresh-libraries",
      {},
    );
    return response.data;
  }
}

export default new JellyfinApi();
