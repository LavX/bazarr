export type SeerrMediaStatus =
  | "unknown"
  | "pending"
  | "processing"
  | "partially_available"
  | "available"
  | "blocklisted"
  | "deleted";
export type SeerrRequestStatus =
  | "pending"
  | "approved"
  | "declined"
  | "failed"
  | "completed";
export type SeerrSeasonState = "requested" | "available" | "requestable";
export type SeerrErrorCode =
  | "not_configured"
  | "unreachable"
  | "rejected_key"
  | "upstream_error"
  | "unresolved";

export interface SeerrRequestSummary {
  id: number;
  status: SeerrRequestStatus;
  is4k: boolean;
  seasons: number[];
}

export interface SeerrMediaState {
  configured: true;
  tmdb_id?: number;
  known: boolean;
  status: SeerrMediaStatus;
  status_4k: SeerrMediaStatus;
  request: SeerrRequestSummary | null;
  seasons: { number: number; state: SeerrSeasonState }[];
  requestable: boolean;
  requestable_4k: boolean;
  partial_requests: boolean;
  special_episodes: boolean;
  link: string | null;
}

export interface SeerrMediaError {
  configured: boolean;
  error_code: SeerrErrorCode;
}

export type SeerrMediaResponse = SeerrMediaState | SeerrMediaError;

export type SeerrIdentity =
  | { kind: "movie"; tmdbId: number }
  | { kind: "tv"; tmdbId: number }
  | { kind: "tv"; tvdbId: number };

export interface SeerrRequestBody {
  media_type: "movie" | "tv";
  tmdb_id: number;
  tvdb_id?: number;
  seasons?: number[] | "all";
  is4k?: boolean;
}

export type SeerrRequestOutcome =
  | {
      outcome: "requested" | "already_requested" | "nothing_to_request";
      request?: SeerrRequestSummary | null;
      link?: string;
    }
  | {
      error_code:
        | "permission"
        | "quota"
        | "blocklisted"
        | "csrf_blocked"
        | "validation"
        | "upstream_error"
        | "rejected_key"
        | "unreachable"
        | "not_configured";
      link?: string;
    };

export interface SeerrTestResult {
  success: boolean;
  version?: string;
  blocklist_capable?: boolean;
  application_title?: string;
  application_url?: string;
  movie_4k?: boolean;
  series_4k?: boolean;
  partial_requests?: boolean;
  special_episodes?: boolean;
  acting_user?: {
    id: number | null;
    display_name: string;
    can_request_movie: boolean;
    can_request_tv: boolean;
    can_request_4k_movie: boolean;
    can_request_4k_tv: boolean;
  };
  error_code?: "configuration" | "connection_failed" | "rejected_key";
}
