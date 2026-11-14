/* eslint-disable camelcase -- transport names. */
import type {
  TranslationConnection,
  TranslationConnectionBody,
  TranslationEngine,
  TranslationHistoryPage,
  TranslationLimitsBody,
  TranslationNotice,
  TranslationOptionsAnswer,
  TranslationSaveBody,
  TranslationState,
  TranslationSummary,
  TranslationTestResult,
} from "@/types/translation";
import BaseApi from "./base";

class TranslationApi extends BaseApi {
  constructor() {
    super("/translation");
  }

  async state() {
    return this.get<TranslationState>("/state");
  }

  async setPointers(body: {
    engine?: number | null;
    fallback_engine?: number | null;
  }) {
    const response = await this.putRaw<{
      engine: number | null;
      fallback_engine: number | null;
      notices: TranslationNotice[];
    }>("/pointers", body);
    return response.data;
  }

  async saveEngine(id: number, body: TranslationSaveBody) {
    const response = await this.patchRaw<{
      engine: TranslationEngine;
      notices: TranslationNotice[];
    }>(`/engines/${id}`, body);
    return response.data;
  }

  async createConnection(instanceId: number, body: TranslationConnectionBody) {
    const response = await this.postRaw<{ connection: TranslationConnection }>(
      `/engines/${instanceId}/connections`,
      body,
    );
    return response.data;
  }

  async updateConnection(id: number, body: TranslationConnectionBody) {
    const response = await this.patchRaw<{ connection: TranslationConnection }>(
      `/connections/${id}`,
      body,
    );
    return response.data;
  }

  /** The revision is the one the caller loaded; the server refuses a removal made from an older view. */
  async deleteConnection(id: number, revision: number) {
    await this.delete(`/connections/${id}`, undefined, {
      expected_revision: revision,
    });
  }

  async reorderConnections(instanceId: number, order: number[]) {
    const response = await this.putRaw<{
      connections: TranslationConnection[];
    }>(`/engines/${instanceId}/connections/order`, { order });
    return response.data;
  }

  async options(
    instanceId: number,
    backendType: string,
    field: string,
    values: object,
  ) {
    const response = await this.postRaw<TranslationOptionsAnswer>(
      `/engines/${instanceId}/options`,
      { backend_type: backendType, field, values },
    );
    return response.data;
  }

  async history(params: { start: number; length: number; status?: string }) {
    return this.get<TranslationHistoryPage>("/history", params);
  }

  /** Spend this month, the caps and the queue. */
  async summary() {
    return this.get<TranslationSummary>("/summary");
  }

  async saveLimits(body: TranslationLimitsBody) {
    const response = await this.putRaw<TranslationSummary>("/limits", body);
    return response.data;
  }

  /** One small request through the back end as it is saved. */
  async testConnection(id: number) {
    const response = await this.postRaw<TranslationTestResult>(
      `/connections/${id}/test`,
      {},
    );
    return response.data;
  }
}

const translation = new TranslationApi();
export default translation;
