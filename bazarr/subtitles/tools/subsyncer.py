# coding=utf-8

import logging
import os
from pathlib import Path
import shutil
import subprocess

from utilities.binaries import get_binary
from radarr.history import history_log_movie
from sonarr.history import history_log
from subtitles.processing import ProcessSubtitlesResult
from subtitles.tools.subsync_engines import (
    DEFAULT_ENABLED_ENGINES,
    OUTPUT_MODE_OVERWRITE,
    SubsyncEngineRunner,
    MissingSyncEngineError,
    normalize_enabled_engines,
    normalize_output_mode,
)
from languages.get_languages import audio_language_from_name, language_from_alpha2
from utilities.path_mappings import path_mappings
from utilities.video_analyzer import subtitles_sync_references
from app.config import settings
from app.database import TableMovies, TableShows, database, select
from app.get_args import args


ENGINE_LABELS = {
    "ffsubsync": "FFsubsync",
    "autosubsync": "Autosubsync",
    "alass": "ALASS",
}


def _autosubsync_model_file():
    from autosubsync import main as autosubsync_main

    return str(
        (
            Path(autosubsync_main.__file__).resolve().parent
            / ".."
            / "trained-model.bin"
        ).resolve()
    )


def _run_autosubsync_api(
    reference, subtitle_file, output_file, model_file, parallelism
):
    from autosubsync.main import synchronize

    return synchronize(
        reference,
        subtitle_file,
        output_file,
        verbose=False,
        model_file=model_file,
        parallelism=parallelism,
    )


class SubSyncer:
    def __init__(self):
        self.reference = None
        self.srtin = None
        self.srtout = None
        self.ffmpeg_path = None
        self.args = None
        try:
            import webrtcvad  # noqa: F401
        except ImportError:
            self.vad = "subs_then_auditok"
        else:
            self.vad = "subs_then_webrtc"
        self.log_dir_path = os.path.join(args.config_dir, "log")
        self.progress_callback = None
        self.sync_result = None
        self.job_id = None

    def _report_progress(self, message, value, total):
        if self.progress_callback:
            self.progress_callback(message, value, total)

    @staticmethod
    def _original_language_name(sonarr_series_id, radarr_id):
        """Read originalLanguage from the local DB. The column is populated by the regular
        Sonarr/Radarr series/movies sync. Returns None if the row is missing or the column
        hasn't been backfilled yet (next series/movies sync will populate it)."""
        try:
            if sonarr_series_id:
                row = database.execute(
                    select(TableShows.originalLanguage).where(
                        TableShows.sonarrSeriesId == int(sonarr_series_id)
                    )
                ).first()
                return row.originalLanguage if row else None
            if radarr_id:
                row = database.execute(
                    select(TableMovies.originalLanguage).where(
                        TableMovies.radarrId == int(radarr_id)
                    )
                ).first()
                return row.originalLanguage if row else None
        except Exception:
            logging.exception(
                "BAZARR could not retrieve originalLanguage from database."
            )
        return None

    @classmethod
    def _audio_stream_for_original_language(
        cls, sonarr_series_id=None, sonarr_episode_id=None, radarr_id=None
    ):
        """Return the ffmpeg audio stream specifier (e.g. 'a:1') matching the show/movie's
        original language as reported by Sonarr/Radarr, or None if not found."""
        logging.debug(
            "BAZARR subsync: looking up original language "
            "(sonarr_series_id=%s, sonarr_episode_id=%s, radarr_id=%s)",
            sonarr_series_id,
            sonarr_episode_id,
            radarr_id,
        )
        target_name = cls._original_language_name(
            sonarr_series_id=sonarr_series_id, radarr_id=radarr_id
        )
        logging.debug(
            "BAZARR subsync: original language reported by Sonarr/Radarr = %r",
            target_name,
        )
        if not target_name:
            return None
        try:
            refs = subtitles_sync_references(
                subtitles_path="",
                sonarr_episode_id=sonarr_episode_id,
                radarr_movie_id=radarr_id,
            )
        except Exception:
            logging.exception(
                "BAZARR could not enumerate audio tracks for original-language matching."
            )
            return None
        audio_tracks = refs.get("audio_tracks", []) if isinstance(refs, dict) else []
        logging.debug(
            "BAZARR subsync: file audio tracks = %s",
            [(t.get("stream"), t.get("language")) for t in audio_tracks],
        )
        # Direct name match (covers most cases)
        for track in audio_tracks:
            if track.get("language") == target_name:
                logging.debug(
                    "BAZARR subsync: matched original language %r to audio track %s",
                    target_name,
                    track.get("stream"),
                )
                return track.get("stream")
        # Bazarr renames a couple of languages internally (Chinese -> Chinese Simplified, Modern Greek -> Greek);
        # try the normalized form as a fallback.
        normalized = audio_language_from_name(target_name)
        if normalized and normalized != target_name:
            for track in audio_tracks:
                if track.get("language") == normalized:
                    logging.debug(
                        "BAZARR subsync: matched normalized original language %r "
                        "(from %r) to audio track %s",
                        normalized,
                        target_name,
                        track.get("stream"),
                    )
                    return track.get("stream")
        logging.debug(
            "BAZARR subsync: original language %r not found in audio tracks; falling back",
            target_name,
        )
        return None

    def _ensure_ffmpeg_path(self):
        ffprobe_exe = get_binary("ffprobe")
        if not ffprobe_exe:
            raise MissingSyncEngineError("ffsubsync", "FFprobe not found")
        logging.debug("BAZARR FFprobe used is %s", ffprobe_exe)

        ffmpeg_exe = get_binary("ffmpeg")
        if not ffmpeg_exe:
            raise MissingSyncEngineError("ffsubsync", "FFmpeg not found")
        logging.debug("BAZARR FFmpeg used is %s", ffmpeg_exe)

        self.ffmpeg_path = os.path.dirname(ffmpeg_exe)
        return self.ffmpeg_path

    def _build_ffsubsync_args(
        self,
        output_path,
        max_offset_seconds,
        no_fix_framerate,
        gss,
        reference=None,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=None,
        force_sync=False,
    ):
        from ffsubsync.ffsubsync import make_parser

        ffmpeg_path = self._ensure_ffmpeg_path()
        unparsed_args = [
            self.reference,
            "-i",
            self.srtin,
            "-o",
            str(output_path),
            "--ffmpegpath",
            ffmpeg_path,
            "--vad",
            self.vad,
            "--log-dir-path",
            self.log_dir_path,
            "--max-offset-seconds",
            max_offset_seconds,
            "--output-encoding",
            "same",
        ]

        if no_fix_framerate:
            unparsed_args.append("--no-fix-framerate")

        if gss:
            unparsed_args.append("--gss")

        logging.debug(
            "BAZARR subsync: settings: force_audio=%s use_original_language=%s "
            "auto_use_original_language=%s force_sync=%s",
            settings.subsync.force_audio,
            settings.subsync.use_original_language,
            settings.subsync.auto_use_original_language,
            force_sync,
        )
        if (
            reference
            and isinstance(reference, str)
            and len(reference) == 3
            and reference[:2] in ["a:", "s:"]
        ):
            unparsed_args.append("--reference-stream")
            unparsed_args.append(reference)
        elif settings.subsync.force_audio and not force_sync:
            stream_spec = "a:0"
            if settings.subsync.use_original_language:
                matched = self._audio_stream_for_original_language(
                    sonarr_series_id=sonarr_series_id,
                    sonarr_episode_id=sonarr_episode_id,
                    radarr_id=radarr_id,
                )
                if matched:
                    stream_spec = matched
            logging.debug("BAZARR subsync: using --reference-stream %s", stream_spec)
            unparsed_args.append("--reference-stream")
            unparsed_args.append(stream_spec)
        elif (
            settings.subsync.use_original_language
            or settings.subsync.auto_use_original_language
        ):
            matched = self._audio_stream_for_original_language(
                sonarr_series_id=sonarr_series_id,
                sonarr_episode_id=sonarr_episode_id,
                radarr_id=radarr_id,
            )
            if matched:
                logging.debug("BAZARR subsync: using --reference-stream %s", matched)
                unparsed_args.append("--reference-stream")
                unparsed_args.append(matched)
            else:
                logging.debug(
                    "BAZARR subsync: no original-language match; using ffsubsync default reference"
                )

        if settings.subsync.debug:
            unparsed_args.append("--make-test-case")

        parser = make_parser()
        return parser.parse_args(args=unparsed_args)

    def _run_ffsubsync_engine(
        self,
        output_path,
        max_offset_seconds,
        no_fix_framerate,
        gss,
        reference=None,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=None,
        force_sync=False,
    ):
        from ffsubsync.ffsubsync import run

        self.args = self._build_ffsubsync_args(
            output_path=output_path,
            max_offset_seconds=max_offset_seconds,
            no_fix_framerate=no_fix_framerate,
            gss=gss,
            reference=reference,
            sonarr_series_id=sonarr_series_id,
            sonarr_episode_id=sonarr_episode_id,
            radarr_id=radarr_id,
            force_sync=force_sync,
        )
        return run(self.args)

    def _run_external_engine(self, engine, output_path, video_path):
        if engine == "autosubsync":
            return self._run_autosubsync_engine(
                output_path=output_path, video_path=video_path
            )

        executable = None
        if engine == "alass":
            executable = shutil.which("alass") or shutil.which("alass-cli")

        if not executable:
            raise MissingSyncEngineError(
                engine, f"{engine} executable not found on PATH"
            )

        reference = (
            self.reference
            if self.reference and os.path.isfile(self.reference)
            else video_path
        )
        command = [executable, reference, self.srtin, str(output_path)]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except subprocess.CalledProcessError as exc:
            details = (
                getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
            ).strip()
            raise RuntimeError(
                f"{engine} failed with exit code {exc.returncode}: {details}"
            ) from exc
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
        }

    def _run_autosubsync_engine(self, output_path, video_path):
        reference = (
            self.reference
            if self.reference and os.path.isfile(self.reference)
            else video_path
        )
        try:
            success = _run_autosubsync_api(
                reference=reference,
                subtitle_file=self.srtin,
                output_file=str(output_path),
                model_file=_autosubsync_model_file(),
                parallelism=1,
            )
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] == "autosubsync":
                raise MissingSyncEngineError(
                    "autosubsync", "autosubsync Python package not installed"
                ) from exc
            raise

        if not success:
            raise RuntimeError(
                "autosubsync completed but did not meet the quality threshold."
            )

        return {
            "success": success,
            "stdout": "",
            "stderr": "",
            "returncode": 0,
        }

    def _log_sync_history(
        self,
        success_result,
        output_mode,
        srt_lang,
        hi,
        forced,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=None,
    ):
        raw_result = (
            success_result.raw_result
            if isinstance(success_result.raw_result, dict)
            else {}
        )
        offset_seconds = raw_result.get("offset_seconds") or 0
        framerate_scale_factor = raw_result.get("framerate_scale_factor") or 0
        message = (
            f"{language_from_alpha2(srt_lang)} subtitles synchronization using "
            f"{success_result.engine} ({output_mode}) ended with an offset of "
            f"{offset_seconds} seconds and a framerate scale factor of "
            f"{f'{framerate_scale_factor:.2f}'}."
        )

        if sonarr_series_id:
            prr = path_mappings.path_replace_reverse
        else:
            prr = path_mappings.path_replace_reverse_movie

        result = ProcessSubtitlesResult(
            message=message,
            reversed_path=prr(self.reference),
            downloaded_language_code2=srt_lang,
            downloaded_provider=None,
            score=None,
            forced=forced,
            subtitle_id=None,
            reversed_subtitles_path=prr(success_result.output_path),
            hearing_impaired=hi,
        )

        if sonarr_episode_id:
            history_log(
                action=5,
                sonarr_series_id=sonarr_series_id,
                sonarr_episode_id=sonarr_episode_id,
                result=result,
            )
        else:
            history_log_movie(action=5, radarr_id=radarr_id, result=result)

    def sync(
        self,
        video_path,
        srt_path,
        srt_lang,
        hi,
        forced,
        max_offset_seconds,
        no_fix_framerate,
        gss,
        reference=None,
        sonarr_series_id=None,
        sonarr_episode_id=None,
        radarr_id=None,
        progress_callback=None,
        job_id=None,
        force_sync=False,
        output_mode=None,
        enabled_engines=None,
        write_history=True,
    ):
        self.reference = video_path
        self.srtin = srt_path
        self.progress_callback = progress_callback
        self.sync_result = None

        if self.srtin.casefold().endswith(".ass"):
            # try to preserve the original subtitle style
            # ffmpeg will be able to handle this automatically as long as it has the libass filter
            extension = ".ass"
        else:
            extension = ".srt"
        self.srtout = f"{os.path.splitext(self.srtin)[0]}.synced{extension}"
        self.args = None
        self.job_id = job_id

        if reference and reference != video_path and os.path.isfile(reference):
            # subtitles path provided
            self.reference = reference

        output_mode = normalize_output_mode(
            output_mode
            or getattr(settings.subsync, "output_mode", OUTPUT_MODE_OVERWRITE)
        )
        enabled_engines = normalize_enabled_engines(
            enabled_engines
            if enabled_engines is not None
            else getattr(settings.subsync, "enabled_engines", DEFAULT_ENABLED_ENGINES)
        )
        progress_total = max(len(enabled_engines), 1)
        engine_positions = {
            engine: index + 1 for index, engine in enumerate(enabled_engines)
        }
        self._report_progress("Preparing synchronization", 0, progress_total)

        def execute_engine(engine, output_path):
            engine_position = engine_positions.get(engine, 1)
            engine_label = ENGINE_LABELS.get(engine, engine)
            self._report_progress(
                f"Running {engine_label} ({engine_position}/{progress_total})",
                engine_position - 1,
                progress_total,
            )
            if engine == "ffsubsync":
                raw_result = self._run_ffsubsync_engine(
                    output_path=output_path,
                    max_offset_seconds=max_offset_seconds,
                    no_fix_framerate=no_fix_framerate,
                    gss=gss,
                    reference=reference,
                    sonarr_series_id=sonarr_series_id,
                    sonarr_episode_id=sonarr_episode_id,
                    radarr_id=radarr_id,
                    force_sync=force_sync,
                )
            else:
                raw_result = self._run_external_engine(
                    engine=engine, output_path=output_path, video_path=video_path
                )
            self._report_progress(
                f"Finished {engine_label} ({engine_position}/{progress_total})",
                engine_position,
                progress_total,
            )
            return raw_result

        runner = SubsyncEngineRunner()
        self.sync_result = runner.run(
            srt_path=self.srtin,
            output_mode=output_mode,
            enabled_engines=enabled_engines,
            execute_engine=execute_engine,
            force_sync=force_sync,
        )

        if settings.subsync.debug:
            return self.sync_result

        if not self.sync_result.success:
            logging.error(f"BAZARR unable to sync subtitles: {self.srtin}")  # noqa: G004
        elif write_history:
            for success_result in self.sync_result.successful_results:
                self._log_sync_history(
                    success_result=success_result,
                    output_mode=output_mode,
                    srt_lang=srt_lang,
                    hi=hi,
                    forced=forced,
                    sonarr_series_id=sonarr_series_id,
                    sonarr_episode_id=sonarr_episode_id,
                    radarr_id=radarr_id,
                )

        return self.sync_result
