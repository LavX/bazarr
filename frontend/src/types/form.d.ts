declare namespace FormType {
  interface ModifyItem {
    id: number[];
    profileid: (number | null)[];
  }

  type SeriesAction = OneSeriesAction | SearchWantedAction | ScanWantedAction;

  type MoviesAction = OneMovieAction | SearchWantedAction | ScanWantedAction;

  interface OneMovieAction {
    action: "search-missing" | "scan-disk" | "sync";
    radarrid: number;
  }

  interface OneSeriesAction {
    action: "search-missing" | "scan-disk" | "sync";
    seriesid: number;
  }

  interface SearchWantedAction {
    action: "search-wanted";
  }

  interface ScanWantedAction {
    action: "scan-wanted";
  }

  interface Subtitle {
    language: string;
    hi: boolean;
    forced: boolean;
  }

  interface UploadSubtitle extends Subtitle {
    file: File;
  }

  interface DeleteSubtitle extends Subtitle {
    path: string;
  }

  interface ModifySubtitle {
    id: number;
    type: "episode" | "movie";
    language: string;
    /** File path. Empty string signals an embedded track, backend extracts it. */
    path: string;
    forced?: PythonBoolean;
    hi?: PythonBoolean;
    original_format?: PythonBoolean;
    reference?: string;
    max_offset_seconds?: string;
    no_fix_framerate?: PythonBoolean;
    gss?: PythonBoolean;
    output_mode?: "overwrite" | "keep_all";
    /** Source language code2; required when path is empty (embedded track). */
    from_language?: string;
    /** HI flag of the embedded source track (defaults to hi when omitted). */
    from_hi?: PythonBoolean;
    /** Forced flag of the embedded source track (defaults to forced when omitted). */
    from_forced?: PythonBoolean;
    /** Owning Sonarr/Radarr instance id (#156); routes the action to the right server. */
    arr_instance_id?: number;
  }

  interface DownloadSeries {
    episodePath: string;
    sceneName?: string;
    language: string;
    hi: boolean;
    forced: boolean;
    sonarrSeriesId: number;
    sonarrEpisodeId: number;
    title: string;
  }

  interface AddBlacklist {
    provider: string;
    subs_id: string;
    language: Language.CodeType;
    subtitles_path: string;
    /** Owning Sonarr/Radarr instance id (#156); routes the action to the right server. */
    arr_instance_id?: number;
  }

  interface DeleteBlacklist {
    provider: string;
    subs_id: string;
    /** Owning Sonarr/Radarr instance id (#156); scopes the removal to one instance. */
    arr_instance_id?: number;
  }

  interface ManualDownload {
    language: string;
    hi: PythonBoolean;
    forced: PythonBoolean;
    provider: string;
    subtitle: unknown;
    original_format: PythonBoolean;
  }

  interface AddAnnouncementsDismiss {
    hash: number;
  }

  interface PlexSelectServer {
    machineIdentifier: string;
    name: string;
    uri: string;
    local: boolean;
    connections?: string[];
  }
}
