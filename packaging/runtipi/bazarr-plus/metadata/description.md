## Bazarr+

Bazarr+ manages subtitles for movie, television, and sports libraries. Use it on its own for supported manual subtitle workflows, or connect media managers that you already run. This Runtipi app installs Bazarr+ only.

### Media paths

Runtipi's shared media directory is mounted at `/media`. Set Bazarr+ library paths to the matching existing paths beneath `/media`, such as `/media/Movies`, `/media/TV`, or `/media/Sports`. This package uses one shared media bind and does not create separate host path selectors for each library.

### Optional services

No external service is required. If you use a translator service or FlareSolverr, configure it in Bazarr+ after installation. This app definition does not install or manage those services.
