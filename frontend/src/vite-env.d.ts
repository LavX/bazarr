/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_KEY: string;
  readonly VITE_CAN_UPDATE: string;
  readonly VITE_HAS_UPDATE: string;
  readonly VITE_QUERY_DEV: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Package resolves via its exports map to a bare CSS file, which the bundler
// resolver does not type as a side-effect import on its own.
declare module "@fontsource-variable/geist";
