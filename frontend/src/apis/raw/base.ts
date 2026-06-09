import { AxiosResponse } from "axios";
import client from "./client";

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

  protected async get<T = unknown>(path: string, params?: LooseObject) {
    const response = await client.axios.get<T>(this.prefix + path, { params });
    return response.data;
  }

  protected post<T = void>(
    path: string,
    formdata?: LooseObject,
    params?: LooseObject,
  ): Promise<AxiosResponse<T>> {
    const form = this.createFormdata(formdata);
    return client.axios.post(this.prefix + path, form, {
      params,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    });
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
