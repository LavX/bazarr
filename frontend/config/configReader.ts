/* eslint-disable no-console */
/// <reference types="node" />

import { readFileSync } from "fs";
import { get } from "lodash";
import { parse } from "yaml";

class ConfigReader {
  config: object;

  constructor() {
    this.config = {};
  }

  open(path: string) {
    try {
      const rawConfig = readFileSync(path, "utf8");
      this.config = parse(rawConfig);
    } catch (err) {
      // We don't want to catch the error here, handle it on getValue method
    }
  }

  getValue(sectionName: string, fieldName: string) {
    const path = `${sectionName}.${fieldName}`;
    const result = get(this.config, path);

    if (result === undefined) {
      throw new Error(`Failed to find ${path} in the local config file`);
    }

    return result;
  }
}

export default function overrideEnv(env: Record<string, string>) {
  const configPath = env["VITE_BAZARR_CONFIG_FILE"];

  if (configPath === undefined) {
    return;
  }

  const reader = new ConfigReader();
  reader.open(configPath);

  if (env["VITE_API_KEY"] === undefined) {
    try {
      const apiKey = reader.getValue("auth", "apikey");

      // Stored secrets are encrypted at rest and only the backend holds the
      // key, so the ciphertext is not an API key. Passing it on sends the dev
      // server into a 401 on every call, which arrives as a login screen with
      // no explanation of why the credentials it already has are being
      // refused.
      if (typeof apiKey === "string" && apiKey.startsWith("enc:v1:")) {
        throw new Error(
          "the API key in the config file is encrypted at rest. Read it from " +
            "the running backend's page (window.Bazarr.apiKey) and set " +
            "VITE_API_KEY in frontend/.env.local",
        );
      }

      env["VITE_API_KEY"] = apiKey;
      process.env["VITE_API_KEY"] = apiKey;
    } catch (err) {
      throw new Error(
        `No API key found, please run the backend first, (error: ${err.message})`,
      );
    }
  }

  if (env["VITE_PROXY_URL"] === undefined) {
    try {
      const port = reader.getValue("general", "port");
      const baseUrl = reader.getValue("general", "base_url");

      const url = `http://127.0.0.1:${port}${baseUrl}`;

      console.log(`Using backend url: ${url}`);

      env["VITE_PROXY_URL"] = url;
      process.env["VITE_PROXY_URL"] = url;
    } catch (err) {
      throw new Error(
        `No proxy url found, please run the backend first, (error: ${err.message})`,
      );
    }
  }
}
