import BaseApi from "./base";

class FilesApi extends BaseApi {
  constructor() {
    super("/files");
  }

  async browse(name: string, path?: string, instanceId?: number) {
    // instance_id (#156) routes the browse at the owning Sonarr/Radarr instance
    // (backend: api/files/files_sonarr.py + files_radarr.py). Omitted => default
    // server, byte-identical to the legacy single-instance behaviour.
    const response = await this.get<FileTree[]>(name, {
      path,
      // eslint-disable-next-line camelcase -- backend API contract
      instance_id: instanceId,
    });
    return response;
  }

  async bazarr(path?: string) {
    return this.browse("", path);
  }

  async sonarr(path?: string, instanceId?: number) {
    return this.browse("/sonarr", path, instanceId);
  }

  async radarr(path?: string, instanceId?: number) {
    return this.browse("/radarr", path, instanceId);
  }
}

const filesApi = new FilesApi();
export default filesApi;
