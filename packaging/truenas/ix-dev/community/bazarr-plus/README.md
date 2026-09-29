# Bazarr+

[Bazarr+](https://lavx.github.io/bazarr/) manages subtitles for movies, TV, and sports. It can run standalone or connect to Sonarr, Radarr, and Sportarr. No translator or FlareSolverr service is required to start.

The app stores its database, configuration, and logs in `/config`. By default, TrueNAS creates an ixVolume for this path. The container starts as root to prepare `/config`, then drops to the User ID and Group ID selected in the form. Its root filesystem is read-only, with temporary files in a tmpfs mounted at `/tmp`.

To let Bazarr+ scan video files and write subtitles beside them, add writable host paths under **Additional Storage**. For example, mount movie, TV, and sports datasets at `/movies`, `/tv`, and `/sports`, then enter those container paths in Bazarr+ when adding libraries or path mappings. The default install does not require any media mount. The selected User ID and Group ID need write permission on each media dataset where subtitle files will be saved.

The web UI listens inside the container on port `6767`. The default host port is `30504` and can be changed in the TrueNAS form.

If migrating from another Bazarr or Bazarr+ installation, stop the old container and back up its config before copying data into this app's `/config`. Do not run both installations against the same config directory. Check the existing container paths in your library settings after migration.
