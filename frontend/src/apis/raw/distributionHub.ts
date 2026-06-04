import BaseApi from "./base";

export interface DistLimitWindows {
  hour: number;
  day: number;
  week: number;
  month: number;
}

export interface DistKindLimits {
  search: DistLimitWindows;
  download: DistLimitWindows;
}

export interface DistKey {
  id: number;
  name: string;
  key_prefix: string | null;
  tier: string;
  tier_label: string;
  enabled: number;
  is_legacy: number;
  timeout_seconds: number | null;
  custom_limits: Partial<DistKindLimits> | null;
  excluded_providers: string[] | null;
  allowed_providers: string[] | null;
  created_at: string | null;
  last_used_at: string | null;
  note: string | null;
  usage?: DistKindLimits;
  limits?: DistKindLimits;
  token?: string;
}

export interface DistKeysResponse {
  keys: DistKey[];
  default_tier: string;
}

export interface DistKeyCreateRequest {
  name: string;
  tier?: string;
  custom_limits?: Partial<DistKindLimits> | null;
  excluded_providers?: string[] | null;
  allowed_providers?: string[] | null;
  timeout_seconds?: number | null;
  note?: string | null;
}

export type DistKeyUpdateRequest = Partial<{
  name: string;
  tier: string;
  enabled: boolean;
  timeout_seconds: number | null;
  note: string | null;
  custom_limits: Partial<DistKindLimits> | null;
  excluded_providers: string[] | null;
  allowed_providers: string[] | null;
}>;

export interface DistTier {
  label: string;
  search: DistLimitWindows;
  download: DistLimitWindows;
}

export interface DistTiersResponse {
  default_tier: string;
  tiers: Record<string, DistTier>;
}

export interface DistOverviewTotals {
  search: number;
  download: number;
}

export interface DistOverview {
  totals: {
    today: DistOverviewTotals;
    d7: DistOverviewTotals;
    d30: DistOverviewTotals;
  };
  blocked_30d: number;
  key_count: number;
  enabled_count: number;
  active_count: number;
  top_keys: {
    key_id: number;
    name: string;
    prefix: string;
    total: number;
  }[];
}

export interface DistTimeseriesPoint {
  date: string;
  search: number;
  download: number;
}

export interface DistTimeseries {
  range_days: number;
  series: DistTimeseriesPoint[];
}

export interface DistSettings {
  enabled: boolean;
  consent: boolean;
  search_timeout_seconds: number;
  search_rate_limit_enabled: boolean;
  usage_retention_days: number;
  default_tier: string;
  downloads_per_window: number;
  downloads_window_seconds: number;
  serve_local_subs: boolean;
  has_token: boolean;
  restart_required?: boolean;
}

class DistributionHubApi extends BaseApi {
  constructor() {
    super("/distribution-hub");
  }

  keys() {
    return this.get<DistKeysResponse>("/keys");
  }

  async createKey(body: DistKeyCreateRequest) {
    const response = await this.postRaw<DistKey>("/keys", body);
    return response.data;
  }

  async updateKey(id: number, body: DistKeyUpdateRequest) {
    const response = await this.patchRaw<DistKey>(`/keys/${id}`, body);
    return response.data;
  }

  deleteKey(id: number) {
    return this.delete(`/keys/${id}`);
  }

  async rotateKey(id: number) {
    const response = await this.postRaw<{ token: string; key_prefix: string }>(
      `/keys/${id}/rotate`,
    );
    return response.data;
  }

  tiers() {
    return this.get<DistTiersResponse>("/tiers");
  }

  async saveTiers(body: {
    default_tier: string;
    tiers: Record<string, DistTier>;
  }) {
    const response = await this.putRaw<DistTiersResponse>("/tiers", body);
    return response.data;
  }

  statsOverview() {
    return this.get<DistOverview>("/stats/overview");
  }

  statsTimeseries(params: { range_days?: number; key_id?: number }) {
    return this.get<DistTimeseries>("/stats/timeseries", params);
  }

  providers() {
    return this.get<{ providers: string[] }>("/providers");
  }

  settings() {
    return this.get<DistSettings>("/settings");
  }

  async saveSettings(body: Partial<DistSettings>) {
    const response = await this.patchRaw<DistSettings>("/settings", body);
    return response.data;
  }

  async regenerate() {
    const response = await this.postRaw<{ ok: boolean; token: string }>(
      "/regenerate",
    );
    return response.data;
  }

  legacyToken() {
    return this.get<{ token: string }>("/legacy-token");
  }
}

export default new DistributionHubApi();
