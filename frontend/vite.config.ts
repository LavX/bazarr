/* eslint-disable camelcase */

/// <reference types="vitest" />
/// <reference types="vite/client" />
/// <reference types="node" />

import react from "@vitejs/plugin-react";
import path from "path";
import { defineConfig, loadEnv } from "vite";
import checker from "vite-plugin-checker";
import { VitePWA } from "vite-plugin-pwa";
import chunks from "./config/chunks";
import overrideEnv from "./config/configReader";

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd());

  if (command === "serve") {
    overrideEnv(env);
  }

  const target = env.VITE_PROXY_URL;
  const ws = env.VITE_ALLOW_WEBSOCKET === "true";
  const secure = env.VITE_PROXY_SECURE === "true";

  const imagesFolder = command === "serve" ? "public/images" : "images";

  return {
    plugins: [
      react(),
      checker({
        typescript: true,
        enableBuild: false,
      }),
      VitePWA({
        workbox: {
          globIgnores: ["index.html"],
          navigateFallback: null,
        },
        registerType: "autoUpdate",
        includeAssets: [
          `${imagesFolder}/favicon.ico`,
          `${imagesFolder}/apple-touch-icon-180x180.png`,
        ],
        manifest: {
          name: "Bazarr",
          short_name: "Bazarr",
          description:
            "Bazarr is a companion application to Sonarr and Radarr. It manages and downloads subtitles based on your requirements.",
          theme_color: "#b36b00",
          icons: [
            {
              src: `${imagesFolder}/pwa-64x64.png`,
              sizes: "64x64",
              type: "image/png",
            },
            {
              src: `${imagesFolder}/pwa-192x192.png`,
              sizes: "192x192",
              type: "image/png",
            },
            {
              src: `${imagesFolder}/pwa-512x512.png`,
              sizes: "512x512",
              type: "image/png",
            },
          ],
          screenshots: [
            {
              src: `/${imagesFolder}/pwa-wide-series-list.jpeg`,
              sizes: "1447x1060",
              label: "Series List",
              form_factor: "wide",
              type: "image/jpeg",
            },
            {
              src: `/${imagesFolder}/pwa-wide-series-overview.jpeg`,
              sizes: "1447x1060",
              label: "Series Overview",
              form_factor: "wide",
              type: "image/jpeg",
            },
            {
              src: `/${imagesFolder}/pwa-narrow-series-list.jpeg`,
              sizes: "491x973",
              label: "Series List",
              form_factor: "narrow",
              type: "image/jpeg",
            },
            {
              src: `/${imagesFolder}/pwa-narrow-series-overview.jpeg`,
              sizes: "491x973",
              label: "Series Overview",
              form_factor: "narrow",
              type: "image/jpeg",
            },
          ],
        },
        devOptions: {
          enabled: mode === "development",
        },
      }),
    ],
    css: {
      preprocessorOptions: {
        scss: {
          api: "modern-compiler",
          additionalData: `
            @use "${path.join(process.cwd(), "src/assets/_mantine").replace(/\\/g, "/")}" as mantine;
            @use "${path.join(process.cwd(), "src/assets/_bazarr").replace(/\\/g, "/")}" as bazarr;
          `,
        },
      },
    },
    base: "./",
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    build: {
      manifest: true,
      sourcemap: mode === "development",
      outDir: "./build",
      chunkSizeWarningLimit: 600,
      rollupOptions: {
        output: {
          manualChunks: chunks,
        },
        external: [
          "fsevents",
          "path",
          "process",
          "perf_hooks",
          "fs/promises",
          "node:path",
          "node:process",
          "node:perf_hooks",
          "node:fs/promises",
        ],
      },
    },
    test: {
      globals: true,
      environment: "jsdom",
      setupFiles: "./src/tests/setup.tsx",
      testTimeout: 20000,
      pool: "forks",
      coverage: {
        // Count the WHOLE src tree, not just files an executed test happens to
        // import. Without an explicit `include`, vitest omits never-imported
        // modules from the report, so brand-new untested code would not lower
        // the "All files" totals and could slip under the floor. The glob makes
        // such files show up at 0% and drag the percentages down as intended.
        include: ["src/**/*.{ts,tsx}"],
        exclude: [
          "src/**/*.d.ts",
          "src/**/__tests__/**",
          "src/tests/**",
          "src/**/*.{test,spec}.{ts,tsx}",
        ],
        // No-regress floor for `vitest run --coverage` (enforced in CI). Set
        // just below the baseline at the time it was added, measured across the
        // full src tree (untested files included).
        // Ratchet these UP as coverage grows; never lower them to make CI pass.
        thresholds: {
          lines: 40,
          statements: 40,
          functions: 35,
          branches: 28,
        },
      },
    },
    server: {
      proxy: {
        "^/(api|images|test)/.*|^/bazarr\\.log$": {
          target,
          changeOrigin: true,
          secure,
          ws,
        },
      },
      host: true,
      open: "/",
    },
  };
});
