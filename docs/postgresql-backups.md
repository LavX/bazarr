# PostgreSQL backups and restores

Bazarr+ backs up and restores a PostgreSQL database with the PostgreSQL client tools, `pg_dump` and
`pg_restore`. They are not optional: without them a backup would hold the configuration file alone, which
looks like a backup in the list and refuses to restore. The official Docker image installs them
(`postgresql-client`). On a bare-metal install, put them on the same PATH the Bazarr+ process sees. On Debian
and Ubuntu that is the `postgresql-client` package, on Alpine `postgresql-client`.

## What a backup contains

| Engine | Database artifact inside the archive | How it is produced |
| --- | --- | --- |
| SQLite | `bazarr.db` | the sqlite3 backup API, against the live database |
| PostgreSQL | `bazarr_postgres.dump` | `pg_dump --format=custom` |

Both archives also carry `config.yaml`. If the database step fails on either engine, no archive is written at
all and the failure is reported: the backup is refused rather than half made.

The connection comes from the same place the application reads it, so a backup always targets the database
the instance is actually using: the `POSTGRES_*` environment variables win over `config.yaml`, and
`POSTGRES_URL` fills in whatever the individual keys leave empty. The password is passed to the client tools
through `PGPASSWORD` in the child environment, never on a command line, which any process on the host can
read.

## Client and server versions

**Keep the client tools at or above the major version of the server.** `pg_dump` refuses outright to dump
from a server newer than itself, so a client older than the server cannot make a backup at all.

The other direction works but is worth knowing about. A dump written by a newer client sets server parameters
an older server has never heard of (`transaction_timeout`, added in PostgreSQL 17, is the one people meet).
`pg_restore` reports those as errors, ignores them, and exits non-zero even though every row was restored.
Bazarr+ recognises that one error class, logs it as a warning naming the client and server major versions, and
treats the restore as successful. Any other error, or any mixture, is still a failure and the restore is
refused.

The Docker image ships whatever `postgresql-client` Debian provides, currently major version 17. A server at
17 or newer sees no warning; a server at 16 or older sees the warning above on every restore.

## What a restore does

PostgreSQL restores with `pg_restore --clean --if-exists --no-owner` into the configured database. SQLite
copies the database file into place and removes the stale write-ahead log beside it.

On both engines the configuration is staged first, the database goes in second, and the configuration is only
swapped into place once the database step worked. A failure aborts before the restart, so an instance never
comes back with a new configuration sitting beside an old database.

One failure cannot be undone and is reported as such. `pg_restore --clean` drops the existing objects before
it loads the new ones, with no transaction around it, so a PostgreSQL restore that fails partway leaves the
database in neither state. The log says so plainly, keeps the extracted dump beside the restore directory as
`bazarr_postgres.dump.failed`, and names the backup folder the archive came from, so the restore can be
retried. That database must not be used until it has been restored again or rebuilt.

## Backups do not cross engines

An archive made on SQLite cannot be restored into a PostgreSQL instance, or the other way round: the dump
format and the database file are not interchangeable. Such an archive is refused with a message naming the
engine this instance runs on and the artifact it expected, before the restart rather than after it. To move
between engines, migrate the data rather than restoring a backup.
