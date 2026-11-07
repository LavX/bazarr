/* eslint-disable camelcase -- transport names. */
import type {
  TranslationConnection,
  TranslationConnectionBody,
  TranslationEngine,
  TranslationNotice,
  TranslationOptionsAnswer,
  TranslationSaveBody,
  TranslationState,
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

  async deleteConnection(id: number) {
    await this.delete(`/connections/${id}`);
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
}

const translation = new TranslationApi();
export default translation;
