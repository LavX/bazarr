# Release-type mismatch detection

## The problem

For smaller subtitle communities a given episode or movie often has subtitles
synchronised to only one release type. The downloader picks a release with no
knowledge of subtitle availability, so it can grab a web release when the only
subtitles that exist are cut for the Blu-ray. The user ends up with no subtitle,
or an unsynchronised one, even though a perfectly good subtitle exists for the
other release type, and nothing in the interface says so.

## What Bazarr+ does about it

When an automatic search finishes without downloading anything for a language,
Bazarr+ looks at the candidates that search already scored and rejected. If none
of them matched the release type of the file on disk, and one of them is cut for
a different, known release type and would have passed the minimum score had the
release type matched, the item is recorded and the user is notified once.

The affected item is flagged in the Wanted list with a "Release mismatch" badge.

Detection only reports. It never blocklists the grabbed release and never
triggers a re-search: doing that can destroy a working file on re-import and can
loop when the downloader grabs the same release type again.

## Enabling it

Settings, Subtitles, "Release Type Mismatch", "Notify About Release Type
Mismatches". **Off by default**, because it produces notifications and an
unasked-for notification stream is worse than the problem it reports.

The notification goes to whatever notifiers are configured, in the same shape as
a download notification.

## What it costs

Nothing in provider traffic. The detector reuses the candidate list of the
search that already ran and issues no request of its own. The only extra work is
parsing the release description of the rejected candidates, and only for a
search that came back empty.

## When it deliberately says nothing

Every rule is biased towards silence, because a detector that fires on every
slightly-off release becomes noise the user turns off, at which point it protects
nothing.

- Any candidate that reached the minimum score, whether or not it was
  downloaded. If an acceptable subtitle existed, the release type is not the
  problem.
- A candidate whose release description does not name a release type, or names
  more than one. Unknown is never treated as evidence.
- Candidates from generated-subtitle providers such as WhisperAI. A
  transcription says nothing about what the communities have released.
- An alternative that would still fall short of the minimum score once the
  release-type points are granted. Only the release-type points are ever
  credited: resolution, release group and codec differences are not.
- A file whose own release type cannot be determined.
- Release types the scorer itself considers one and the same. Bazarr+ awards
  the release-type match through `MERGED_FORMATS`, which groups Blu-ray,
  Ultra HD Blu-ray and HD-DVD together, HDTV with SDTV, DVD with VHS, and so
  on. The detector buckets candidates exactly the same way, so it never
  reports a difference the score never punished.
- Anything already recorded. A repeated scheduled pass over the same wanted item
  notifies nothing.
- Upgrade searches. They raise the minimum score above the score of the
  subtitle the user already has, so every candidate is rejected by
  construction and nothing is actually missing.

A detection is identified by the item, its owning Sonarr or Radarr instance, the
searched language and the release type the file itself is. Re-grabbing the item
as a different release type is a new situation and may be reported once more.

## Storage

Detections live in the `release_type_mismatches` table, added by migration
`a3f1c7d90b21`. Records carry the local media id and the owning
`arr_instance_id`, so they stay correct across multiple Sonarr or Radarr
instances.
