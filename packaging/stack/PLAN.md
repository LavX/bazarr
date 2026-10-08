# Combined stack

Ship one Compose package containing Bazarr+, AI Subtitle Translator, and FlareSolverr. Keep the existing standalone packages available.

1. Pin published images and confirm their actual startup and configuration contracts.
2. Bootstrap fresh storage with internal companion URLs and a generated shared translator key. Preserve saved settings on subsequent starts.
3. Keep companion ports inside the stack network. Expose only Bazarr's web interface.
4. Test fresh startup, authenticated translator connectivity, FlareSolverr requests, restart persistence, and refusal to overwrite unrelated existing configuration.
5. Independently review the package, then test a native platform import in an isolated machine.

No production replacement, provider account registration, paid translation request, catalog submission, commit, or release is included.
