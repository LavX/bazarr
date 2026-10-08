# CasaOS native validation

Validated on 2026-09-29 through CasaOS's native `casaos-cli app-management` Compose-file route. This was a local import test, not App Store catalog ingestion or publication.

Use the [CasaOS-ready manifest](../casaos/Apps/BazarrPlusStack/docker-compose.yml). Its SHA-256 is `4410ed7d2e13bd60663e4d7dd44f68fd16dcac244f667bb0bbc924f6c01d3e05`.

## Import compatibility

- Native dry-run and initial install succeeded with separate app ID `bazarr-plus-stack`. After resolving the CasaOS-specific defaults below, apply and restart also reached a healthy stack.
- CasaOS rejected the generic bind expression `${BAZARR_BIND_IP:-0.0.0.0}` during dry-run (`Invalid ip address: $BAZARR_BIND_IP`). The CasaOS manifest uses a literal `0.0.0.0:6767:6767` binding.
- CasaOS apply preserved `${PUID:-1000}` and `${PGID:-1000}` literally, preventing Bazarr startup. The CasaOS manifest therefore pins PUID/PGID to `1000` and TZ to `UTC`; the initializer arguments use matching numeric IDs. For custom IDs, update the initializer IDs and Bazarr PUID/PGID together.
- The test fixture changed only the host port and `x-casaos.port_map` to `6768`, leaving the existing standalone app on `6767` untouched.

## Runtime evidence

- After native apply and restart, Bazarr+, AI Subtitle Translator, and FlareSolverr all reported healthy. The one-shot initializer exited `0` with Docker healthcheck disabled (`Test: ["NONE"]`). Its existing configuration and translator key volumes survived apply and restart.
- Only Bazarr was host-published in the test. Translator `8765` and FlareSolverr `8191-8192` remained container-only.
- The supplied credential-safe probe passed after restart: authenticated Bazarr translator status `200`, unauthenticated translator request `401`, FlareSolverr health `200`, and FlareSolverr browser request to Bazarr `200`. Nine provider helper URLs were seeded; onboarding remained incomplete and no paid or translation requests were made.
- No media was mounted, no external provider or account was contacted, and no OpenRouter key was configured. The native package's declared security options remain in the manifest.
