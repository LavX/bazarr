export function blacklistParams({
  includeSources,
  sourceKeys,
}: Blacklist.Scope) {
  return {
    sources: includeSources ? 1 : undefined,
    // eslint-disable-next-line camelcase
    source_key: sourceKeys,
  };
}

export function blacklistRemovalRequest(removal: Blacklist.Removal) {
  if ("sourceBlacklistId" in removal) {
    return {
      params: {
        sources: 1,
        // eslint-disable-next-line camelcase
        source_blacklist_id: removal.sourceBlacklistId,
        // eslint-disable-next-line camelcase
        source_blacklist_key: removal.sourceBlacklistKey,
      },
      form: undefined,
    };
  }

  if (removal.all) {
    return {
      params: { all: true, ...blacklistParams(removal) },
      form: undefined,
    };
  }

  return { params: { all: false }, form: removal.form };
}
