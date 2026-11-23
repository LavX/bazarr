declare namespace SocketIO {
  type EventType = NumEventType | NullEventType | SpecialEventType;

  type NumEventType =
    | "movie"
    | "series"
    | "episode"
    | "episode-history"
    | "episode-wanted"
    | "movie-wanted"
    | "source-item"
    | "source-group";

  type NullEventType =
    | "sports"
    | "connect"
    | "connect_error"
    | "disconnect"
    | "episode-blacklist"
    | "movie-blacklist"
    | "movie-history"
    | "reset-episode-wanted"
    | "reset-movie-wanted"
    | "badges"
    | "backup"
    | "task"
    | "settings"
    | "languages";

  type SpecialEventType = "jobs" | "session" | "source-removed";

  type ActionType = "update" | "delete";

  type PayloadType = number | string | CustomEvent.SourceRemoved;

  type ReducerGroup<
    E extends EventType,
    U extends PayloadType | undefined,
    D = U,
  > = ValueOf<{
    [P in E]: {
      key: P;
      any?: ActionHandler<undefined>;
      update?: ActionHandler<U>;
      delete?: ActionHandler<D>;
    };
  }>;

  type Event = {
    type: EventType;
    action: ActionType;
    payload?: PayloadType;
  };

  type ActionHandler<T> = T extends undefined
    ? () => void
    : (payload: T[]) => void;

  type Reducer =
    | ReducerGroup<NumEventType, number>
    | ReducerGroup<NullEventType, undefined>
    | ReducerGroup<"jobs", CustomEvent.Jobs>
    | ReducerGroup<"session", string>
    | ReducerGroup<"source-removed", CustomEvent.SourceRemoved>;

  type ActionRecord = {
    [P in EventType]?: {
      [R in ActionType]?: PayloadType[];
    };
  };

  namespace CustomEvent {
    type Jobs = Manager.Jobs;
    type SourceRemoved = { kinds: ("movie" | "episode" | "sports_event")[] };
  }
}
