export const isDevEnv = import.meta.env.MODE === "development";
export const isProdEnv = import.meta.env.MODE === "production";
export const isTestEnv = import.meta.env.MODE === "test";

function injectedEnvironment() {
  if (typeof window === "undefined") {
    return undefined;
  }

  return window.Bazarr;
}

function normalizeBaseUrl(url?: string) {
  if (!url) {
    return "";
  }

  return url.endsWith("/") ? url.slice(0, -1) : url;
}

export const Environment = {
  get apiKey(): string | undefined {
    const injected = injectedEnvironment();
    if (injected?.apiKey) {
      return injected.apiKey;
    }

    if (isDevEnv) {
      return import.meta.env.VITE_API_KEY;
    } else if (isTestEnv) {
      return undefined;
    } else {
      return undefined;
    }
  },
  get canUpdate(): boolean {
    const injected = injectedEnvironment();
    if (injected?.canUpdate !== undefined) {
      return injected.canUpdate;
    }

    if (isDevEnv) {
      return import.meta.env.VITE_CAN_UPDATE === "true";
    } else if (isTestEnv) {
      return false;
    } else {
      return false;
    }
  },
  get hasUpdate(): boolean {
    const injected = injectedEnvironment();
    if (injected?.hasUpdate !== undefined) {
      return injected.hasUpdate;
    }

    if (isDevEnv) {
      return import.meta.env.VITE_HAS_UPDATE === "true";
    } else if (isTestEnv) {
      return false;
    } else {
      return false;
    }
  },
  get baseUrl(): string {
    const injected = injectedEnvironment();
    if (injected) {
      return normalizeBaseUrl(injected.baseUrl);
    }

    if (isDevEnv || isTestEnv) {
      return "";
    } else {
      return "";
    }
  },
  get queryDev(): boolean {
    if (isDevEnv) {
      return import.meta.env.VITE_QUERY_DEV === "true";
    }
    return false;
  },
};
