import { AxiosRequestConfig, AxiosResponse } from "axios";
import client from "./client";

// The request flag the password confirm modal sets: the caller reports the
// failure itself (an inline message), so the shared error handler stays quiet
// for the statuses it handles. Everything else still toasts as usual. The
// signingIn and signingOut flags are declared next to the client that reads
// them.
export type SilentRequestConfig = AxiosRequestConfig & {
  silentError?: boolean;
};

class BaseApi {
  prefix: string;

  constructor(prefix: string) {
    this.prefix = prefix;
  }

  private createFormdata(object?: LooseObject) {
    if (object) {
      const form = new FormData();

      for (const key in object) {
        const data = object[key];
        if (data instanceof Array) {
          if (data.length > 0) {
            data.forEach((val) => form.append(key, val));
          } else {
            form.append(key, "");
          }
        } else if (data !== undefined && data !== null) {
          // Skip undefined/null so an optional field (e.g. from_language on a
          // non-embedded translate) is omitted rather than appended as the literal
          // string "undefined", which the backend would reject.
          form.append(key, data);
        }
      }
      return form;
    } else {
      return undefined;
    }
  }

  protected async get<T = unknown>(
    path: string,
    params?: LooseObject,
    signal?: AbortSignal,
  ) {
    const response = await client.axios.get<T>(this.prefix + path, {
      params,
      signal,
    });
    return response.data;
  }

  // Full response (not just data): callers need headers like
  // Content-Disposition to name the file they save.
  protected getBlob(
    path: string,
    params?: LooseObject,
  ): Promise<AxiosResponse<Blob>> {
    return client.axios.get<Blob>(this.prefix + path, {
      params,
      responseType: "blob",
    });
  }

  protected post<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
    options?: {
      silentError?: boolean;
      signingOut?: boolean;
      signingIn?: boolean;
    },
  ): Promise<AxiosResponse<T>> {
    const form = this.createFormdata(formdata);
    return client.axios.post(this.prefix + path, form, {
      params,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      // A caller that surfaces the failure itself asks the shared client to
      // stay quiet for the statuses it handles inline; see the interceptor.
      silentError: options?.silentError === true,
      // A sign-out request never announces a sign-in from its own answer.
      signingOut: options?.signingOut === true,
      // A login request is the one answer that may announce a sign-in while
      // the sign-out block is in place.
      signingIn: options?.signingIn === true,
    } as SilentRequestConfig);
  }

  protected postRaw<T = void>(
    path: string,
    data?: unknown,
    params?: LooseObject,
    headers?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    return client.axios.post(this.prefix + path, data, { params, headers });
  }

  protected patch<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    const form = this.createFormdata(formdata);
    return client.axios.patch(this.prefix + path, form, { params });
  }

  protected patchRaw<T = void>(
    path: string,
    data?: unknown,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    return client.axios.patch(this.prefix + path, data, { params });
  }

  protected putRaw<T = void>(
    path: string,
    data?: unknown,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    return client.axios.put(this.prefix + path, data, { params });
  }

  protected delete<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    const form = this.createFormdata(formdata);
    return client.axios.delete(this.prefix + path, { params, data: form });
  }
}

export default BaseApi;
