# PostgreSQL backups and restores

Bazarr+ uses the PostgreSQL client tools `pg_dump`, `pg_restore`, and `psql` for complete backups and
verified restores. The official Docker image installs them
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

Newer clients can emit a `transaction_timeout` setting that servers before PostgreSQL 17 do not support.
Bazarr applies that setting only when the server supports it. A SQL error in the restored dump stops the
transaction and leaves the staged files available for recovery.

The Docker image ships the `postgresql-client` provided by its Debian base. Run `pg_dump --version`
inside the container to see the installed version.

## What a restore does

For PostgreSQL, Bazarr records a receipt in the selected database when it stages a restore. It uses
`pg_restore --clean --if-exists --no-owner` to generate the SQL and `psql` to apply it. The same transaction
checks the receipt before applying the dump and marks it complete before committing. The PostgreSQL client
package includes both tools. Regular Bazarr backups omit the receipt table.

SQLite installs a prepared database file and removes the write-ahead log and shared memory files from the old
database. Bazarr tracks the file it replaces and the file it installs.

On both engines the configuration is staged first, the database goes in second, and the configuration is only
swapped into place once the database step worked. A failure aborts before the restart, so an instance never
comes back with a new configuration sitting beside an old database.

An older restore that failed partway may leave `bazarr_postgres.dump.failed` in the restore directory.
That restore dropped existing objects before loading the new ones without a transaction. Restore the
database from the retained dump or rebuild it before starting Bazarr again.

A restore refused before `pg_restore` was ever started is a different thing and says so. Missing client
tools, no database name configured and an unparseable connection URL all stop the restore before the
database is touched, and the log says nothing was changed and names what to fix.

### Recover an interrupted restore

Keep the effective database destination the same until Bazarr finishes restarting and applying the
backup's plugin files. If startup cannot verify the recorded database, restore the original connection
settings, including environment overrides, and start Bazarr again. The staged files remain available.

If Bazarr stops during restore, it keeps `.restore-journal.json`, the dump, the staged configuration, and any
staged plugin data. A committed PostgreSQL receipt lets Bazarr resume on the original database. When the
transaction has rolled back, wait for the original `psql` process to exit, then set only the journal's `phase`
to `staged` to retry on the original database.

For a restore without a receipt, first make sure the original restore process has exited. Complete or verify
the database restore before setting the journal's `phase` to `database-complete`. On SQLite, stop Bazarr and
move any `bazarr.db-wal` and `bazarr.db-shm` from the old database aside before verifying the replacement file.
If `bazarr.db.restore` remains beside it, rename that prepared file to `bazarr.db` so its recorded file identity
is preserved. Start Bazarr with the original settings. It checks the destination before installing the
configuration and applying plugin files.

## Backups do not cross engines

An archive made on SQLite cannot be restored into a PostgreSQL instance, or the other way round: the dump
format and the database file are not interchangeable. Such an archive is refused with a message naming the
engine this instance runs on and the artifact it expected, before the restart rather than after it. To move
between engines, migrate the data rather than restoring a backup.
