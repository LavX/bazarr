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

The connection comes from the same resolver the application uses, so a backup always targets the database
the instance is actually using. Each of the user, password, host, port and database comes from the first of
these that sets it: its `POSTGRES_*` environment variable (`POSTGRES_USERNAME`, `POSTGRES_PASSWORD`,
`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`), then `POSTGRES_URL`, then `config.yaml`. With a URL
set, `config.yaml` only fills in what the URL leaves out, so its default `localhost` and `5432` no longer
replace the URL's host and port, and a URL naming a `service` takes nothing from it. An environment variable
set to an empty string changes nothing in the URL.
Without a URL, the five settings are used as before. The password is passed to the client tools
through `PGPASSWORD` in the child environment, never on a command line, which any process on the host can
read.

## Client and server versions

**Keep the client tools at or above the major version of the server.** `pg_dump` dumps a server of its own
major version or older, and refuses outright to dump from a server newer than itself, so a client older than
the server cannot make a backup at all.

That rule is about making the dump. Restoring is where the other direction shows up, and it works but is
worth knowing about. A dump written by a newer client sets server parameters
an older server has never heard of (`transaction_timeout`, added in PostgreSQL 17, is the one people meet).
`pg_restore` reports those as errors, ignores them, and exits non-zero even though every row was restored.
Bazarr+ recognises that one error class, logs it as a warning naming the client and server major versions, and
treats the restore as successful. Any other error, or any mixture, is still a failure and the restore is
refused.

The Docker image ships whatever `postgresql-client` its Debian base provides, currently major version 17. Run
`pg_dump --version` inside the container to see the one you have. With a 17 client, a server at 17 backs up
and restores without a warning, a server at 16 or older backs up and shows the warning above on every
restore, and a server at 18 or newer cannot be backed up at all.

## What a restore does

PostgreSQL restores with `pg_restore --clean --if-exists --no-owner` into the configured database. SQLite
stages a complete database file in the database directory before replacing the live file and removes the stale
write-ahead log.

On both engines the configuration is staged first, the database goes in second, and the configuration is only
swapped into place once the database step worked. A failure aborts before the restart, so an instance never
comes back with a new configuration sitting beside an old database.

One failure cannot be undone and is reported as such. `pg_restore --clean` drops the existing objects before
it loads the new ones, with no transaction around it, so a PostgreSQL restore that fails partway leaves the
database in neither state. The log names the backup folder and the extracted dump at
`restore/.backup-staged/payload/bazarr_postgres.dump.failed` for System Backups, or
`restore/bazarr_postgres.dump.failed` for a manual extraction. An interrupted PostgreSQL restore without
plugin data also uses that failed-dump path. Confirm the old restore process has exited before retrying.
Keep the database offline until it has been restored again or rebuilt.

A restore refused before `pg_restore` was ever started is a different thing and says so. Missing client
tools, no database name configured and an unparseable connection URL all stop the restore before the
database is touched, and the log says nothing was changed and names what to fix.

### Recover an interrupted plugin data restore

An accepted restore from System Backups is staged under `restore/.backup-staged/payload`. A backup extracted
manually for restore can still use the top level of `restore`. Temporary extraction directories whose names
start with `.backup-extract-` are ignored on startup. Keep the original ZIP until the restore finishes.

Bazarr+ installs the database and configuration first. On the next start it waits for any previous plugin
workers to stop and checks the archived release folders against the plugin inventory before installing them.
If the folder step fails, the staged files are retained and the next start retries the folder step.
For backups with plugin data, the restored configuration keeps the current database connection settings so
the next boot can read the restored inventory. Keep the effective environment and libpq settings the same
until the restore finishes.
Before restoring an archive with plugin folders, Bazarr+ checks that the PostgreSQL dump contains a single
plugin inventory in the schema the configured connection reads. If the schema cannot be matched, Bazarr+
keeps the staged archive and stops before changing the database.

The `.plugin-restore-phase` checkpoint covers PostgreSQL restores and SQLite restores with plugin data. It sits
beside the API extraction directory or at the top level of `restore` for a manual extraction. If it says
`database-started`, the last database operation did not record completion. Keep the extracted files and make
sure the old restore process has exited. To retry the
database restore, put the original dump and configuration back into the extraction directory, remove any
`bazarr_postgres.dump.failed` and remove the checkpoint before starting Bazarr+. To continue after manually
completing or verifying the same database restore, keep the staged configuration at
`config/config.yaml.restore`, move any failed dump aside and set the checkpoint to `database-complete` before
starting Bazarr+.

## Backups do not cross engines

An archive made on SQLite cannot be restored into a PostgreSQL instance, or the other way round: the dump
format and the database file are not interchangeable. Such an archive is refused with a message naming the
engine this instance runs on and the artifact it expected, before the restart rather than after it. To move
between engines, migrate the data rather than restoring a backup.
