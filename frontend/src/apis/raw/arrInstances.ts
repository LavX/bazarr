import BaseApi from "./base";

export type ArrKind = "sonarr" | "radarr";

export interface ArrInstance {
  id: number;
  kind: ArrKind;
  stable_key: string;
  name: string;
  display_name: string;
  enabled: boolean;
  is_default: boolean;
  ip: string;
  port: number;
  base_url: string;
  ssl: boolean;
  verify_ssl: boolean;
  http_timeout: number;
  // The API never returns the key itself, only whether one is stored.
  api_key_set: boolean;
}

export interface ArrInstanceCreate {
  kind: ArrKind;
  name: string;
  api_key?: string;
  ip?: string;
  port?: number;
  base_url?: string;
  ssl?: boolean;
  verify_ssl?: boolean;
  http_timeout?: number;
  enabled?: boolean;
  is_default?: boolean;
}

export type ArrInstanceUpdate = Partial<{
  name: string;
  // Omit api_key to preserve the stored key; set clear_api_key to wipe it.
  api_key: string;
  clear_api_key: boolean;
  ip: string;
  port: number;
  base_url: string;
  ssl: boolean;
  verify_ssl: boolean;
  http_timeout: number;
  enabled: boolean;
  is_default: boolean;
}>;

export interface ArrInstanceTest {
  kind: ArrKind;
  api_key?: string;
  ip?: string;
  port?: number;
  base_url?: string;
  ssl?: boolean;
  verify_ssl?: boolean;
  http_timeout?: number;
}

// Connection overrides for testing a saved instance. The kind and the stored
// API key come from the row server-side, so neither is sent here.
export type ArrInstanceTestOverrides = Partial<{
  ip: string;
  port: number;
  base_url: string;
  ssl: boolean;
  verify_ssl: boolean;
  http_timeout: number;
}>;

export interface ArrInstanceTestResult {
  ok: boolean;
  version?: string;
  app_name?: string;
  error?: string;
  message?: string;
}

class ArrInstancesApi extends BaseApi {
  constructor() {
    super("/system/arr-instances");
  }

  list(kind?: ArrKind) {
    return this.get<ArrInstance[]>("", kind ? { kind } : undefined);
  }

  getOne(id: number) {
    return this.get<ArrInstance>(`/${id}`);
  }

  async create(body: ArrInstanceCreate) {
    const response = await this.postRaw<ArrInstance>("", body);
    return response.data;
  }

  async update(id: number, body: ArrInstanceUpdate) {
    const response = await this.patchRaw<ArrInstance>(`/${id}`, body);
    return response.data;
  }

  remove(id: number) {
    return this.delete(`/${id}`);
  }

  // The API key travels in the JSON body only, never in a URL or query string.
  async test(body: ArrInstanceTest) {
    const response = await this.postRaw<ArrInstanceTestResult>("/test", body);
    return response.data;
  }

  // Tests a saved instance with its stored key (decrypted server-side). Used by
  // the card "Test" and the edit modal's "Keep current key" mode, where the
  // plaintext key is never available in the browser. Optional overrides let an
  // unsaved edit form test the on-screen connection values with the stored key.
  async testExisting(id: number, overrides?: ArrInstanceTestOverrides) {
    const response = await this.postRaw<ArrInstanceTestResult>(
      `/${id}/test`,
      overrides ?? {},
    );
    return response.data;
  }
}

export default new ArrInstancesApi();
