type BlacklistRow = Blacklist.Movie | Blacklist.Episode;

export function blacklistRowId(row: BlacklistRow, index: number) {
  // Several Arr exclusions can share a media ID.
  return row.origin === "source"
    ? `source:${row.id}`
    : `arr:${row.id}:${index}`;
}

export function blacklistRowRemoval(row: BlacklistRow): Blacklist.Removal {
  if (row.origin === "source") return { sourceBlacklistId: row.id };
  return {
    all: false,
    form: {
      provider: row.provider,
      // eslint-disable-next-line camelcase
      subs_id: row.subs_id,
      // eslint-disable-next-line camelcase
      arr_instance_id: row.arr_instance_id,
    },
  };
}
