import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("png");
Config.setOverwriteOutput(true);
// The composition is one canvas element; concurrency above the core count just
// thrashes. Left at the default so the renderer picks per-machine.
