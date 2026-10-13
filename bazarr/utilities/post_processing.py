# coding=utf-8

import os
import re
import sys
import shlex
import ntpath
import logging

from app.config import settings


_PLACEHOLDER = re.compile(r'{{(\w+)}}')
# The $ forms a shell used to expand: a command or parameter substitution
# $(...) ${...} $name, and the special parameters $@ $# $? $$ $! $* $- $0
# onwards, which arrive as a literal argument without a shell.
_POSIX_EXPANSION = re.compile(r'\$[({\w@#?$!*-]')
_WINDOWS_VARIABLE = re.compile(r'%[^%"\s]+%')
_POSIX_ASSIGNMENT = re.compile(r'[ \t]*[A-Za-z_]\w*=')
# nice accepts its adjustment as a bare positional operand, so a number there
# is data and any other word is the program itself.
_NUMERIC_OPERAND = re.compile(r'[+-]?[0-9]+(?:\.[0-9]+)?\Z')
# Programs that run the command given after their own arguments.
_WRAPPERS = ('nice', 'nohup', 'timeout', 'sudo', 'doas', 'ionice', 'stdbuf', 'setsid', 'xargs', 'flock',
             'busybox', 'chrt', 'taskset', 'runuser', 'gosu', 'su-exec', 's6-setuidgid', 'watch', 'su')
# What each wrapper consumes before the program it runs: the options that take
# a separate value operand, the positional operands (timeout's duration, gosu's
# user spec) that come before the wrapped program, and the options that remove
# the positional operand (taskset -c supplies the mask itself). A value or an
# operand is not the program, so neither may satisfy the fixed-program check
# and let a placeholder take the program position. The 'numeric' operand is
# nice's optional bare adjustment: a number there is data, any other word is
# the program itself.
_WRAPPER_SPECS = {
    'nice': (('-n', '--adjustment'), 'numeric', ()),
    'nohup': ((), 0, ()),
    'timeout': (('-k', '--kill-after', '-s', '--signal', '-d', '--duration'), 1, ()),
    # sudo: -S reads the password from standard input, a flag that takes no
    # value, so it stays out of the value options; -h and its --host long
    # name the host the command runs on, so they carry the host behind
    # them.
    'sudo': (('-u', '--user', '-h', '--host', '-g', '--group',
              '-p', '--prompt', '-r', '--role', '-t', '--type',
              '-C', '--close-from', '-D', '--chdir', '-R', '--chroot',
              '-T', '--command-timeout', '--other-user'), 0, ()),
    'doas': (('-u', '-a', '-C', '-L'), 0, ()),
    # ionice: -t is the ignore flag and reads nothing; -c and -n take the
    # class and the level.
    'ionice': (('-c', '--class', '-n', '--classdata'), 0, ()),
    'stdbuf': (('-o', '--output', '-e', '--error', '-i', '--input'), 0, ()),
    'setsid': ((), 0, ()),
    'xargs': (('-I', '-L', '-n', '-P', '-s', '-E', '-a', '--arg-file',
               '-d', '--delimiter', '--max-args', '--max-chars', '--max-procs',
               '--process-slot-var'), 0, ()),
    'flock': (('-w', '--wait', '--timeout', '-W', '-E', '--conflict-exit-code',
               '-c', '--command'), 1, ()),
    'busybox': ((), 0, ()),
    # chrt: the deadline policy reads its runtime, period and deadline from
    # their three options, by their long names and their short letters
    # alike, before the priority operand and the command; the pid mode
    # (-p, --pid) names a process to adjust and never runs one.
    'chrt': (('-T', '--sched-runtime', '-P', '--sched-period',
               '-D', '--sched-deadline'), 1, ()),
    'taskset': (('-c', '--cpu-list'), 1, ('-c', '--cpu-list')),
    'runuser': (('-u', '--user', '-g', '--group', '-G', '--supp-group',
                 '-c', '--command', '--session-command', '-s', '--shell',
                 '-w', '--whitelist-environment'),
                1, ('-u', '--user')),
    'gosu': ((), 1, ()),
    'su-exec': ((), 1, ()),
    's6-setuidgid': ((), 1, ()),
    # watch: --interval, --equexit and --shotsdir take a value, by their
    # long names and their short letters alike; -e, -x and --exec are
    # flags, so the program sits in the program position behind them.
    'watch': (('-n', '--interval', '-q', '--equexit',
               '-s', '--shotsdir'), 0, ()),
    # su: the text after -c and --command runs through the target account's
    # shell, so a placeholder there is shell code. The wrapper rules refuse
    # one before any fixed program appears, which covers the -c operand with
    # and without -s naming that shell; -s only selects it.
    'su': (('-c', '--command', '--session-command', '-s', '--shell', '-g', '--group',
            '-G', '--supp-group', '-w', '--whitelist-environment'), 1, ()),
}
# The wrapper options whose value attaches to their own spelling only, the
# GNU optional-argument form: the installed findutils xargs reads its
# deprecated -e eof string, -i replace string and -l line count from behind
# their letters (-eXX, -i{}, -l2) and reads no separate argument behind a
# bare spelling, so a walk resolves them to their names without awaiting one
# and the program the wrapper runs keeps its position behind them.
_WRAPPER_OPTIONAL_VALUES = {'xargs': ('-e', '-i', '-l')}
# Every shell runs the command text given after -c, so they all go through the
# same validation as bash: ash covers busybox ash on its own and the shells
# busybox runs, csh and tcsh are the C shells, and rbash is bash restricted in
# what it may do, not in how -c command text reaches it.
_SHELLS = ('sh', 'ash', 'bash', 'rbash', 'dash', 'zsh', 'ksh', 'fish', 'csh', 'tcsh')

# Versioned interpreter names (perl5.38.2, ruby3.4, php8.3, node20) run the
# same interpreters with the version pinned, so the trailing version is folded
# off to look up the per-family rules; the typed name itself still runs
# exactly as given.
_VERSION_SUFFIX = re.compile(r'[0-9.]+$')


def _interpreter_family(name):
    return _VERSION_SUFFIX.sub('', name)

# Commands cmd.exe runs itself. They are not programs an argument list can
# start, so a template naming one can never run; the ones that copy or move
# files have executable equivalents to point the user at.
_WINDOWS_INTERNAL = frozenset((
    'assoc', 'call', 'cd', 'chdir', 'cls', 'color', 'copy', 'date', 'del', 'dir',
    'echo', 'endlocal', 'erase', 'exit', 'for', 'ftype', 'goto', 'if', 'md',
    'mkdir', 'mklink', 'move', 'path', 'pause', 'popd', 'prompt', 'pushd', 'rd',
    'rem', 'ren', 'rename', 'rmdir', 'set', 'setlocal', 'shift', 'start',
    'time', 'title', 'type', 'ver', 'verify', 'vol',
))

# Shell built-ins with no executable of their own on a normal POSIX system. Like
# the cmd.exe set above they are not programs an argument list can start, so a
# template naming one can never run; the dot command is the '.' ntpath leaves
# alone. echo, kill, printf and test keep real executables and stay allowed.
_POSIX_INTERNAL = frozenset((
    '.', 'alias', 'bg', 'builtin', 'cd', 'chdir', 'declare', 'dirs', 'disown',
    'enable', 'eval', 'export', 'fc', 'fg', 'getopts', 'hash', 'help',
    'history', 'jobs', 'let', 'local', 'logout', 'popd', 'pushd', 'read',
    'readonly', 'return', 'set', 'shift', 'shopt', 'source', 'suspend',
    'times', 'trap', 'type', 'typeset', 'ulimit', 'umask', 'unalias', 'unset',
    'wait',
))

# Programs that take their program text as the first positional argument: awk
# and its relatives, sed and jq. A provider-supplied value in that slot would
# become executable code (awk's system() runs commands), so the program text
# must be fixed and the placeholders go in as file or positional data after
# it. Each spec is (message label, value options, options whose value operand
# comes in two tokens, simple flags, flags that make the rest positional data,
# options that take an optional attached value). The simple flags and the
# optional-value options are the complete short sets of the installed tools
# (gawk 5.3, GNU sed 4.9, jq 1.8), so a cluster of them reads the way the
# tool itself parses it. The -f and -e value options carry the program
# themselves, so what follows them is input files.
_AWK_SPEC = ('awk',
             ('-e', '-E', '-f', '-F', '-i', '-l', '-v', '-W', '--file', '--field-separator',
              '--assign', '--source', '--exec', '--include', '--load'),
             (),
             ('-b', '-c', '-C', '-g', '-h', '-I', '-k', '-M', '-N', '-n', '-O', '-P', '-r', '-s',
              '-S', '-t', '-V', '--characters-as-bytes', '--traditional', '--copyright',
              '--gen-pot', '--help', '--trace', '--csv', '--bignum', '--use-lc-numeric',
              '--non-decimal-data', '--optimize', '--posix', '--re-interval', '--no-optimize',
              '--sandbox', '--lint-old', '--version'),
             (),
             ('-d', '-D', '-L', '-o', '-p'))
_POSITIONAL_CODE = dict.fromkeys(('awk', 'gawk', 'mawk', 'nawk'), _AWK_SPEC)
_POSITIONAL_CODE.update({
    'sed': ('sed',
            ('-e', '-f', '-l', '--expression', '--file', '--line-length'),
            (),
            ('-n', '-E', '-r', '-s', '-z', '-u', '-b', '-c', '--debug', '--sandbox',
             '--follow-symlinks', '--posix', '--quiet', '--silent', '--copy', '--binary',
             '--regexp-extended', '--separate', '--unbuffered', '--null-data', '--help',
             '--version'),
            (),
            ('-i',)),
    'jq': ('jq',
           ('-f', '-L', '--from-file', '--library-path', '--indent'),
           ('--arg', '--argjson', '--rawfile', '--slurpfile', '--jsonslurpfile'),
           ('-e', '-n', '-r', '-R', '-s', '-c', '-j', '-a', '-S', '-C', '-M', '-V', '-h',
            '--tab', '--stream', '--stream-errors', '--seq', '--slurp', '--raw-input',
            '--raw-output', '--raw-output0', '--null-input', '--compact-output', '--join-output',
            '--exit-status', '--sort-keys', '--monochrome-output', '--color-output',
            '--ascii-output', '--unbuffered', '--version', '--build-configuration', '--help'),
           ('--args', '--jsonargs'),
           ()),
})
# The value options that carry the program itself, so a fixed value for one
# of them supplies the program and what follows them is input files.
_PROGRAM_TEXT_OPTIONS = frozenset(('-e', '-E', '-f', '--file', '--expression', '--from-file',
                                  '--source', '--exec'))
# One cross-program set of option names whose value or operand the program
# executes, interprets as code, or resolves as a script or program path:
# sed's -e/--expression and awk's -f/--file make their value the program,
# GNU tar's --checkpoint-action runs its exec action, --use-compress-program
# and its short form -I name a program tar runs itself, --to-command pipes
# each extracted file to a command, and --info-script and
# --new-volume-script name the scripts tar runs at volume changes, find's
# -exec/-execdir/-ok/-okdir build and run a command, sort's
# --compress-program names the program it runs to compress its temporary
# output, and --command, --script, --source, --program, --eval and their
# kin are the generic spellings of the same position, with e and f as the
# short forms that share a bundle token with other letters. rsync runs the
# --rsync-path value on the remote side and the --rsh value as the
# transport shell, both through a shell, and -e is rsync's short spelling of
# --rsh, which the generic e letter already carries. tar reaches a remote
# archive through the shell its --rsh-command names and runs the tape
# program its --rmt-command names on the far side, so both select a
# program it executes. The set is matched for
# every program alike, known or unknown, because a placeholder value comes
# from subtitle and provider metadata and a program that gives one of these
# names a plain data meaning cannot be told from one that executes it, so
# the value is refused for all of them.
_EXECUTING_OPTION_NAMES = frozenset((
    'e', 'f', 'exec', 'execdir', 'ok', 'okdir', 'execute', 'expression', 'file',
    'command', 'cmd', 'script', 'source', 'program', 'prog', 'eval',
    'checkpoint-action', 'use-compress-program', 'to-command', 'compress-program',
    'info-script', 'new-volume-script', 'interpreter', 'shell', 'run',
    'rsync-path', 'rsh', 'rsh-command', 'rmt-command',
))
# The short carriers are the single-letter names above; a short bundle that
# contains one of them is a carrier too, the way sed reads -ne as -n and -e.
_EXECUTING_OPTION_LETTERS = frozenset(
    name for name in _EXECUTING_OPTION_NAMES if len(name) == 1)
# A few programs document short options the case-blind letter match would
# misread, so each carries its own arity, matched for the exact letter:
# each spec is (value-less flags, executing carriers, options whose value
# is plain data). rsync's -E (executability) and -F (its --filter
# shortcut) are flags, so the generic reading would let either eat the
# element behind it as the value of the -e/-f carrier its lowercase spells,
# and hide a live carrier there, while lowercase -e stays the --rsh carrier
# the letter set already carries. The value-less set is the complete short
# flag list of the installed rsync 3.5.1, its -a archive mode among them,
# so a documented flag, and a cluster of documented letters (-av), reads
# nothing behind it and a bare -- right behind one stays the separator;
# rsync's -T reads its directory operand, its -f the filter rule, its -B
# the block size and its -@ the modification-time window as plain data
# the way tar's -C does, glued to the letter (-T/tmp, -frule) or in the
# next argument, and its -M, the short spelling of --remote-option, joins
# them: the option it forwards to the remote side to parse there is data
# to the local scan however it is spelled, with the carrier letter e
# alone staying out and keeping its own reading. tar's
# -I is the short form of --use-compress-program, a carrier its lowercase
# never spells (tar's own -i is the --ignore-zeros flag), so the i letter
# stays out of the cross-program set. tar's value-less short letters join
# the flag set complete, the documented list of the installed GNU tar 1.35
# (--verbose's -v and --compress's -Z among them), so a documented tar
# flag and a cluster of documented letters (-cv) read nothing behind them
# and a bare -- right behind one stays the separator. The value-taking
# short options of the installed GNU tar 1.35 that read plain data join
# the set complete: -C reads a directory, -T the file list filename, -X
# the exclusion-pattern file list, -K the member the reading starts at,
# -g the listed-incremental snapshot file, -N the date or file it compares
# against, -V the volume label and -H the archive format, glued to the
# letter (-C/media, -Tlist) or in the next argument, while tar's -f stays
# the generic file carrier it is for every program and tar's -F, -b and
# -L stay out with their values. sort's value-less short letters join
# the flag set complete, the documented list of the installed coreutils
# sort 9.10, --ignore-case's -f among them, the lowercase letter the
# cross-program file carrier itself spells: the documented letter
# resolves ahead of the cross-program reading, alone and in a cluster of
# documented letters (-bf), so a documented sort flag reads nothing
# behind it and a bare -- right behind one stays the separator. sort's -o
# reads the filename it writes its output to and its -T the directory it
# keeps its temporary files in, plain data however the name is spelled,
# glued to the letter or in the next argument, so an operand that looks
# like an option name is that filename or directory; the short options
# whose operand must parse as a value (-S's size, -t's single separator
# character, -k's key definition) stay out, because an option-shaped
# operand never parses as their value.
_PROGRAM_SHORT_ARITY = {
    'rsync': (('v', 'q', 'c', 'a', 'r', 'R', 'b', 'u', 'd', 'l', 'L', 'k', 'K', 'H',
               'p', 'E', 'A', 'X', 'o', 'g', 'D', 't', 'U', 'N', 'O', 'J', 'S', 'n',
               'W', 'x', 'm', 'I', 'y', 'z', 'C', 'F', '0', 's', '8', 'h', 'P', 'i',
               '4', '6', 'V'), (), ('T', 'f', 'B', '@', 'M')),
    'tar': (('A', 'c', 'd', 'r', 't', 'u', 'x', 'G', 'n', 'S', 'k', 'U', 'W', 'O', 'm',
             'p', 's', 'M', 'B', 'i', 'a', 'j', 'J', 'z', 'Z', 'h', 'P', 'l', 'R', 'v',
             'w', 'o', '?'), ('I',), ('C', 'T', 'X', 'K', 'g', 'N', 'V', 'H')),
    'sort': (('b', 'd', 'f', 'g', 'i', 'M', 'h', 'n', 'R', 'r', 'V', 'c', 'C', 'm',
              's', 'u', 'z'), (), ('o', 'T')),
}
# Long options a known program documents as value-less flags, held the way
# the short arity table holds its programs' letters: rsync's --archive, the
# long spelling of its -a archive mode, reads no value behind it, so a
# bare -- right behind it is the separator, not a value the flag would
# take. The set is the complete value-less long option list of the
# installed rsync 3.5.1, its symlink transforms (--copy-links among
# them), its -z compression, its --checksum quick check and the daemon's
# --no-detach included, and the options that do read a value (--rsh among
# them) staying out of it, and a long option the table leaves undocumented
# keeps the GNU-convention reading a bare -- right behind it takes as that
# option's separate value. tar's entry is the complete value-less long
# option list of the installed GNU tar 1.35 too, its operating modes,
# modifiers and --no- negations included (--verbose and --compress among
# them), so a documented tar flag reads nothing behind it and is never
# the carrier its name prefixes: --compress is not --compress-program
# and --checkpoint is not --checkpoint-action. The spellings whose
# optional argument attaches with the separator and never comes from the
# next argument (--checkpoint[=NUMBER], --occurrence[=NUMBER],
# --backup[=CONTROL], --atime-preserve[=METHOD], --one-top-level[=DIR],
# --totals[=SIGNAL]) hold the same value-less reading, their attached
# value ignored the way every attached value is. sort's entry is the
# complete value-less long option list of the installed coreutils sort
# 9.10 as well, its ordering flags and --help/--version included
# (--stable and --ignore-case among them), so a documented sort flag
# reads nothing behind it and is never the carrier its name prefixes
# either: --check is the check flag, not --checkpoint-action, and it
# joins the attached-value spellings too (--check=diagnose-first,
# --check=quiet, --check=silent, the words its -c and -C short letters
# carry), its optional argument attaching with the separator and never
# coming from the next argument.
_PROGRAM_LONG_VALUELESS_NAMES = {'rsync': frozenset((
    '8-bit-output', 'acls', 'append', 'append-verify', 'archive', 'atimes',
    'backup', 'blocking-io', 'checksum', 'compress', 'copy-devices',
    'copy-dirlinks', 'copy-links', 'copy-unsafe-links', 'crtimes',
    'cvs-exclude', 'daemon', 'del', 'delay-updates', 'delete', 'delete-after',
    'delete-before', 'delete-delay', 'delete-during', 'delete-excluded',
    'delete-missing-args', 'detect-moved', 'detect-renamed',
    'detect-renamed-lax', 'devices', 'dirs', 'drop-D', 'dry-run',
    'executability', 'existing', 'fake-super', 'force', 'from0', 'fsync',
    'fuzzy', 'group', 'hard-links', 'help', 'human-readable', 'i-r',
    'ignore-errors', 'ignore-existing', 'ignore-missing-args',
    'ignore-non-existing', 'ignore-times', 'inc-recursive', 'inplace',
    'insecure-links', 'ipv4', 'ipv6', 'itemize-changes', 'keep-dirlinks',
    'links', 'list-only', 'mkpath', 'munge-links', 'no-D', 'no-W',
    'no-detach', 'no-i-r', 'no-implied-dirs', 'no-inc-recursive', 'no-motd',
    'no-whole-file', 'numeric-ids', 'old-args', 'old-d', 'old-dirs',
    'omit-dir-times', 'omit-link-times', 'one-file-system', 'open-noatime',
    'owner', 'partial', 'perms', 'preallocate', 'progress',
    'prune-empty-dirs', 'quiet', 'recursive', 'relative',
    'remove-source-files', 'safe-links', 'secluded-args', 'size-only',
    'sparse', 'specials', 'stats', 'super', 'times', 'trust-sender',
    'update', 'verbose', 'version', 'whole-file', 'write-devices', 'xattrs',
)), 'tar': frozenset((
    'absolute-names', 'acls', 'anchored', 'append', 'atime-preserve', 'auto-compress',
    'backup', 'block-number', 'bzip2', 'catenate', 'check-device', 'check-links', 'checkpoint',
    'clamp-mtime', 'compare', 'compress', 'concatenate', 'confirmation', 'create',
    'delay-directory-restore', 'delete', 'dereference', 'diff', 'exclude-backups',
    'exclude-caches', 'exclude-caches-all', 'exclude-caches-under', 'exclude-vcs',
    'exclude-vcs-ignores', 'extract', 'force-local', 'full-time', 'get', 'gunzip', 'gzip',
    'hard-dereference', 'help', 'ignore-case', 'ignore-command-error', 'ignore-failed-read',
    'ignore-zeros', 'incremental', 'interactive', 'keep-directory-symlink', 'keep-newer-files',
    'keep-old-files', 'list', 'lzip', 'lzma', 'lzop', 'multi-volume', 'no-acls', 'no-anchored',
    'no-auto-compress', 'no-check-device', 'no-delay-directory-restore', 'no-ignore-case',
    'no-ignore-command-error', 'no-null', 'no-overwrite-dir', 'no-recursion', 'no-same-owner',
    'no-same-permissions', 'no-seek', 'no-selinux', 'no-unquote', 'no-verbatim-files-from',
    'no-wildcards', 'no-wildcards-match-slash', 'no-xattrs', 'null', 'numeric-owner',
    'occurrence', 'old-archive', 'one-file-system', 'one-top-level', 'overwrite',
    'overwrite-dir', 'portability', 'posix', 'preserve-order', 'preserve-permissions',
    'read-full-records', 'recursion', 'recursive-unlink', 'remove-files', 'restrict',
    'same-order', 'same-owner', 'same-permissions', 'seek', 'selinux', 'show-defaults',
    'show-omitted-dirs', 'show-snapshot-field-ranges', 'show-stored-names',
    'show-transformed-names', 'skip-old-files', 'sparse', 'test-label', 'to-stdout', 'totals',
    'touch', 'uncompress', 'ungzip', 'unlink-first', 'unquote', 'update', 'usage', 'utc',
    'verbatim-files-from', 'verbose', 'verify', 'version', 'wildcards',
    'wildcards-match-slash', 'xattrs', 'xz', 'zstd',
)), 'sort': frozenset((
    'check', 'debug', 'dictionary-order', 'general-numeric-sort', 'help',
    'human-numeric-sort', 'ignore-case', 'ignore-leading-blanks',
    'ignore-nonprinting', 'merge', 'month-sort', 'numeric-sort', 'random-sort',
    'reverse', 'stable', 'unique', 'version', 'version-sort', 'zero-terminated',
))}
# Long options a known program gives a plain data meaning, whose operand is
# consumed as data the way a carrier's own value is: rsync's pattern and
# file-list options (--exclude, --include, --exclude-from, --include-from,
# --files-from) read their pattern or filename operand as plain data, and
# tar's --label reads the next element as the literal archive label, its
# --files-from the file list filename, and its --newer family (--newer,
# --after-date, --newer-mtime) the date or file it compares against. The
# sets are the complete documented data-option lists of the installed
# rsync 3.5.1, GNU tar 1.35 and coreutils sort 9.10, every remaining
# option whose operand is plain data included: rsync's directory, mapping,
# rate, batch, log and tuning options among them, tar's exclusion,
# attribute, transformation, format, quoting and output ones, and sort's
# paths. The options whose operand the program executes stay out as the
# carriers they are (rsync's --rsh and --rsync-path, tar's --to-command
# and --checkpoint-action families, sort's --compress-program). rsync's
# --remote-option and sort's path operands join the data set, each data
# however it is spelled: --remote-option forwards its operand to the
# remote side to parse there, so it is data to the local scan, sort's
# --output reads the file it writes its output to, --temporary-directory
# the directory it keeps its temporary files in, --random-source the
# file it seeds its random hash from and --files0-from the file of the
# input names, while sort's options whose operand must parse as a value
# (--buffer-size's size, --field-separator's single separator character,
# --key's key definition, --parallel's and --batch-size's numbers and
# --sort's word) stay out, because an option-shaped operand never parses
# as their value.
_PROGRAM_DATA_OPERAND_NAMES = {
    'rsync': ('exclude', 'include', 'exclude-from', 'include-from', 'files-from',
              'filter', 'suffix', 'temp-dir', 'block-size', 'modify-window',
              'chmod', 'chown', 'usermap', 'groupmap', 'copy-as', 'confine-root',
              'backup-dir', 'partial-dir', 'compare-dest', 'copy-dest', 'link-dest',
              'max-delete', 'max-size', 'min-size', 'max-alloc', 'skip-compress',
              'checksum-choice', 'cc', 'checksum-seed', 'compress-choice', 'zc',
              'compress-level', 'zl', 'compress-threads', 'zt', 'bwlimit', 'timeout',
              'contimeout', 'stop-after', 'stop-at', 'iconv', 'outbuf', 'out-format',
              'log-file', 'log-file-format', 'info', 'debug', 'stderr', 'address',
              'port', 'sockopts', 'protocol', 'password-file', 'early-input',
              'config', 'write-batch', 'only-write-batch', 'read-batch',
              'remote-option'),
    'tar': ('label', 'files-from', 'newer', 'after-date', 'newer-mtime',
            'directory', 'exclude', 'exclude-from', 'exclude-ignore',
            'exclude-ignore-recursive', 'exclude-tag', 'exclude-tag-all',
            'exclude-tag-under', 'add-file', 'starting-file', 'listed-incremental',
            'volno-file', 'index-file', 'mtime', 'transform', 'xform', 'mode',
            'owner', 'owner-map', 'group', 'group-map', 'sort', 'suffix', 'format',
            'pax-option', 'hole-detection', 'warning', 'quoting-style',
            'quote-chars', 'no-quote-chars', 'xattrs-exclude', 'xattrs-include'),
    'sort': ('output', 'temporary-directory', 'random-source', 'files0-from'),
}


def _shell_syntax_error(feature):
    if feature == '\n':
        feature = 'a line break'
    elif feature == '#':
        feature = "a '#' comment"
    elif feature in ('$', '~', '*'):
        feature = f"'{feature}' expansion"
    elif len(feature) == 1:
        feature = f"'{feature}'"
    return ValueError(f'Commands run without a shell, so {feature} is not supported. Put pipes, '
                      f'redirects, && and variables in a script and pass the placeholders to it as arguments')


def _reject_shell_syntax(command, windows):
    """Refuse syntax a shell used to interpret, which would now arrive as a literal argument."""
    if windows:
        # cmd.exe toggles quoting on every double quote and expands variables inside quotes too.
        quoted = False
        for index, char in enumerate(command):
            if char == '"':
                quoted = not quoted
            elif not quoted and char in '|&<>^\n':
                raise _shell_syntax_error(char)
            elif not quoted and char in '()' and (
                    index == 0 or command[index - 1] in ' \t' or
                    index + 1 >= len(command) or command[index + 1] in ' \t'):
                # A grouping parenthesis only worked because cmd.exe read
                # it; without a shell it names a program '(' instead.
                raise _shell_syntax_error(char)
        variable = _WINDOWS_VARIABLE.search(command)
        if variable:
            raise _shell_syntax_error(f"'{variable[0]}'")
        return

    if _POSIX_ASSIGNMENT.match(command):
        raise _shell_syntax_error('a leading variable assignment')
    if '\\\n' in command:
        # A backslash before a line break used to make the shell join the
        # lines into one word; the direct argument list keeps the line break
        # and the command changes meaning, so the template is refused.
        raise ValueError('Commands run without a shell, so a backslash before a line break is not '
                         'supported. The shell joined the lines there and direct execution does '
                         'not, so write the command as one line')
    index = 0
    word_start = True
    while index < len(command):
        char = command[index]
        if char == '\\':
            index += 2
        elif char == "'":
            end = command.find("'", index + 1)
            # An unclosed quote is reported by the parser.
            index = len(command) if end < 0 else end + 1
        elif char == '"':
            index += 1
            while index < len(command) and command[index] != '"':
                if command[index] == '\\':
                    index += 2
                    continue
                if command[index] == '`' or _POSIX_EXPANSION.match(command, index):
                    raise _shell_syntax_error(command[index])
                index += 1
            index += 1
        elif char in '()':
            # A grouping parenthesis only worked because a shell read it;
            # without one, a word starting with '(' names a program '('.
            if (char == '(' and word_start) or (char == ')' and (
                    index + 1 >= len(command) or command[index + 1] in ' \t')):
                raise _shell_syntax_error(char)
            word_start = False
            index += 1
            continue
        elif char in '|&;<>`\n*?[]' or _POSIX_EXPANSION.match(command, index) or (char in '~#' and word_start):
            raise _shell_syntax_error(char)
        else:
            word_start = char in ' \t'
            index += 1
            continue
        word_start = False


def _split_command(command, windows=False):
    """Parse the configured template before any external metadata is inserted."""
    if not windows:
        return shlex.split(command)

    # Windows CRT argument rules: backslashes are literal except immediately
    # before a double quote. Single quotes have no special meaning on Windows.
    args = []
    i = 0
    while i < len(command):
        while i < len(command) and command[i] in ' \t':
            i += 1
        if i == len(command):
            break
        value = []
        quoted = False
        while i < len(command):
            slashes = 0
            while i < len(command) and command[i] == '\\':
                slashes += 1
                i += 1
            if i < len(command) and command[i] == '"':
                value.append('\\' * (slashes // 2))
                if slashes % 2:
                    value.append('"')
                elif quoted and i + 1 < len(command) and command[i + 1] == '"':
                    value.append('"')
                    i += 1
                else:
                    quoted = not quoted
                i += 1
                continue
            value.append('\\' * slashes)
            if i == len(command) or (command[i] in ' \t' and not quoted):
                break
            value.append(command[i])
            i += 1
        if quoted:
            raise ValueError('Unclosed quote in post-processing command')
        arg = ''.join(value)
        # The old shell template stripped single quotes around a placeholder on Windows too.
        if len(arg) >= 2 and arg[0] == arg[-1] == "'" and _PLACEHOLDER.search(arg):
            arg = arg[1:-1]
        args.append(arg)
    return args


def _validate_interpreter_template(args, *, code_options=(), script_options=(),
                                   value_options=(), simple_options=(), shell_short_options=False,
                                   code_texts=None, placeholders_after_code=True):
    """Allow metadata only after a fixed script/module or static code argument."""
    code_options = {option.lower() for option in code_options}
    code_texts = dict(code_texts or {})
    script_options = {option.lower() for option in script_options}
    value_options = {option.lower() for option in value_options}
    simple_options = {option.lower() for option in simple_options}
    index = 1
    while index < len(args):
        arg = args[index]
        lower = arg.lower()
        if lower in code_options:
            if index + 1 >= len(args) or _PLACEHOLDER.search(args[index + 1]):
                # Perl's -e and -E differ only in case, so match the exact argument first.
                named = code_texts.get(arg) or code_texts.get(lower)
                if named:
                    raise ValueError(f'Pass subtitle metadata as arguments to a fixed script file, '
                                     f'not {named}')
                # Defensive catch-all: every caller names its code options above.
                raise ValueError('Pass subtitle metadata as arguments to a fixed script file')
            if not placeholders_after_code:
                # The code text is fixed, but what follows it is not plain
                # argv for this interpreter: node, perl and ruby keep
                # parsing options there (a second --eval= or -e runs more
                # code) and a PowerShell command swallows the rest of the
                # line as command text. A placeholder could then carry
                # provider-supplied metadata in as an option or into the
                # command, so metadata may not follow the code text.
                for later in args[index + 2:]:
                    if _PLACEHOLDER.search(later):
                        raise ValueError('Pass subtitle metadata as arguments to a fixed script '
                                          'file, not after inline code')
            return
        if shell_short_options and arg.startswith('-') and not arg.startswith('--') and 'c' in arg[1:]:
            if index + 1 >= len(args) or _PLACEHOLDER.search(args[index + 1]):
                raise ValueError('Pass subtitle metadata as positional arguments, not sh -c command text')
            return
        if lower in script_options:
            if index + 1 >= len(args) or _PLACEHOLDER.search(args[index + 1]):
                raise ValueError('Post-processing script or module path must be fixed')
            return
        if lower in value_options:
            if index + 1 >= len(args) or _PLACEHOLDER.search(args[index + 1]):
                raise ValueError('Post-processing interpreter options must be fixed')
            index += 2
            continue
        attached = next((option for option in value_options
                         if lower.startswith(option + '=') or
                         (len(option) == 2 and lower.startswith(option) and len(lower) > 2)), None)
        if attached is not None:
            if _PLACEHOLDER.search(arg):
                raise ValueError('Post-processing interpreter options must be fixed')
            index += 1
            continue
        if arg == '--':
            index += 1
            if index >= len(args) or _PLACEHOLDER.search(args[index]):
                raise ValueError('Post-processing script path must be fixed')
            return
        if lower in simple_options or (shell_short_options and arg.startswith('-') and
                                        not arg.startswith('--') and set(arg[1:]) <= set('eufxvnl')):
            index += 1
            continue
        if arg.startswith('-'):
            raise ValueError('Use a direct interpreter with fixed script options for post-processing')
        if _PLACEHOLDER.search(arg):
            raise ValueError('Post-processing script path must be fixed')
        return
    raise ValueError('Pass subtitle metadata after a fixed script file')


def _clustered_value_option(arg, values, pairs, simple, optional):
    """Read a short-option token the way GNU option parsing does.

    Simple flags may share one token with a value option behind them and
    that option's value attached after the flags (-ne'script'): the flags
    are consumed one letter at a time and the option behind them takes the
    rest of the token as its value. An option with an optional attached
    value (sed -i, gawk -d) keeps the rest of the token the same way, and
    with nothing behind it, it is a plain flag. Returns the option, its
    attached value and whether the token parses at all; the value is None
    when the token ends at a mandatory value option, so that option takes
    its operand from the next argument instead, and the option is None for
    plain flags and for a letter the installed tools do not take, which
    unparsed tells apart.
    """
    short_flags = {option[1] for option in simple if len(option) == 2}
    short_optional = {option[1] for option in optional if len(option) == 2}
    short_values = {option[1] for option in (*values, *pairs) if len(option) == 2}
    for position, char in enumerate(arg[1:], 1):
        if char in short_values:
            return '-' + char, arg[position + 1:] or None, False
        if char in short_optional:
            return '-' + char, arg[position + 1:], False
        if char not in short_flags:
            return None, None, True
    return None, None, False


def _validate_positional_code_template(args, label, values, pairs, simple, flags, optional):
    """Refuse a placeholder where the program text itself goes.

    awk and its relatives, sed and jq take their program as the first
    positional argument, so metadata in that slot would be executed. The
    program text must be fixed, and so must the options before it. After the
    program a placeholder is file or positional data, unless it directly
    follows an option and would be that option's value.
    """
    index = 1
    program_from_options = False
    positional_only = False
    while index < len(args):
        arg = args[index]
        if arg in flags:
            positional_only = True
            index += 1
            continue
        if arg in values or arg in pairs:
            count = 1 if arg in values else 2
            for operand in args[index + 1:index + 1 + count]:
                if _PLACEHOLDER.search(operand):
                    raise ValueError(f'The {label} options must be fixed; pass the placeholders to it '
                                      f'as file arguments')
            if arg in _PROGRAM_TEXT_OPTIONS:
                program_from_options = True
            index += 1 + count
            continue
        if arg.startswith('-') and len(arg) > 1:
            # GNU option parsing also accepts the value attached to its
            # option (-escript, --expression=script, -fscript, -Fsep), and
            # simple flags may share the token in front of it with the
            # value attached behind them (-ne'script'), so the value of a
            # known option can hide inside one argument. A placeholder
            # there is the option's value, program text for the
            # program-carrying options, and is refused exactly as the
            # detached operand is; a fixed attached value of those options
            # supplies the program, so the next token never becomes it.
            if arg.startswith('--'):
                option = next((candidate for candidate in (*values, *pairs)
                               if arg.startswith(candidate + '=')), None)
                attached_value = arg[len(option) + 1:] if option is not None else None
                unparsed = option is None
            else:
                option, attached_value, unparsed = _clustered_value_option(arg, values, pairs,
                                                                            simple, optional)
            if unparsed and _PLACEHOLDER.search(arg):
                # The token mixes placeholder metadata with option letters or
                # names beyond what the installed tools document, so the
                # metadata may sit in a value position the real parser
                # understands even though this model does not.
                raise ValueError(f'The {label} options must be fixed; pass the placeholders to it '
                                  f'as file arguments')
            if option is None:
                # An option without a placeholder is skipped; a value operand
                # it takes from the next argument lands in the program
                # position below, where a placeholder is refused anyway.
                index += 1
                continue
            if attached_value is None:
                # The token ends at the option, so it takes its value operand
                # from the next argument, exactly like the detached form.
                count = 1 if option in values else 2
                for operand in args[index + 1:index + 1 + count]:
                    if _PLACEHOLDER.search(operand):
                        raise ValueError(f'The {label} options must be fixed; pass the placeholders '
                                          f'to it as file arguments')
                index += count
            elif _PLACEHOLDER.search(arg):
                raise ValueError(f'The {label} options must be fixed; pass the placeholders to it '
                                  f'as file arguments')
            if option in _PROGRAM_TEXT_OPTIONS:
                program_from_options = True
            index += 1
            continue
        break
    if not program_from_options:
        if index >= len(args) or _PLACEHOLDER.search(args[index]):
            raise ValueError(f'The {label} program text must be fixed; pass the placeholders to it '
                             f'as file arguments')
        index += 1
    previous = None
    for arg in args[index:]:
        if arg in flags:
            positional_only = True
        elif _PLACEHOLDER.search(arg) and not positional_only and previous is not None \
                and previous.startswith('-'):
            raise ValueError(f'The {label} options must be fixed; pass the placeholders to it '
                             f'as file arguments')
        previous = arg


# find's predicates that read one plain data argument, the -name pattern
# among them: the argument is data to find, so the span walk consumes it
# with its predicate and an operand that looks like a payload option never
# starts a span where find reads data. find's data actions read a count
# of their own, so they carry it in _FIND_DATA_ACTION_ARITY and consume
# the same way; the tests that read no argument (-nouser, -nogroup)
# stay out, because consuming for them would misalign the walk again.
# -samefile reads one filename operand the same way, and the -newerXY
# tests each read one reference operand, X naming the file's a/B/c/m
# timestamp and Y the reference's a/B/c/m/t one (X=t is invalid per the
# installed findutils), so the family joins the table complete; the match
# is on the lowered token, so the family is held lowercased with the
# birth-time letter.
_FIND_DATA_PREDICATES = frozenset((
    '-name', '-iname', '-lname', '-ilname', '-path', '-ipath', '-wholename',
    '-iwholename', '-regex', '-iregex', '-user', '-group', '-uid', '-gid',
    '-perm', '-type', '-xtype', '-links', '-inum', '-size', '-used',
    '-atime', '-ctime', '-mtime', '-amin', '-cmin', '-mmin', '-newer',
    '-anewer', '-cnewer', '-fstype', '-context', '-samefile',
)) | {f'-newer{x}{y}' for x in 'abcm' for y in 'abcmt'}

# The plain data arguments each of find's data actions reads: -printf's
# one argument is its format, -fprintf reads the file it prints to
# before the format, and -fprint, -fprint0 and -fls read the file they
# print their output to. The span walk and the carrier scan both consume
# those arguments with the action, so an operand that looks like a
# payload option is the data find reads and never starts a span or a
# carrier reading of its own.
_FIND_DATA_ACTION_ARITY = {'-printf': 1, '-fprintf': 2, '-fprint': 1, '-fprint0': 1, '-fls': 1}

# find's operand-reading options outside the tests and actions:
# -files0-from reads the file it takes its starting points from and
# -regextype the regex syntax the -regex and -iregex tests parse their
# patterns with, one plain data argument each, and -D reads the debug
# flags it prints its diagnostics under, warning and continuing past a
# flag it does not recognise, through the case-sensitive match in
# _find_data_arity: -d is the value-less -depth spelling, and the lowered
# match the set uses would make it read an operand -depth never takes.
# The number-taking options (-maxdepth, -mindepth) stay out: their operand
# is a number, an element that looks like an action is not the number
# find reads there and find refuses the command before running anything
# when one stands in, so the walks cannot hide a payload behind their
# operand either way.
_FIND_DATA_GLOBAL_OPTIONS = frozenset(('-files0-from', '-regextype'))


def _find_data_arity(token):
    """The plain data arguments a find data test, action or option reads, or
    None.

    The two walks the module makes over find's arguments, the span walk
    locating what -exec runs and the carrier scan reading the options,
    consume a data predicate's, data action's or data option's operands
    with its token by this count, so an operand that looks like an option
    is the data find reads instead. -D reads its debug flags as one such
    plain argument and find only warns about a flag it does not
    recognise, so an operand that looks like a payload option is -D's
    data too; the -D match is case-sensitive, because -d is find's
    value-less -depth spelling and must not consume an operand.
    """
    if token == '-D':
        return 1
    lowered = token.lower()
    if lowered in _FIND_DATA_PREDICATES or lowered in _FIND_DATA_GLOBAL_OPTIONS:
        return 1
    return _FIND_DATA_ACTION_ARITY.get(lowered)


def _find_payload_spans(args):
    """Each payload-taking option's span, as (start, end), with end pointing
    at the terminator or past the last argument.

    -exec and its relatives (-execdir, -ok, -okdir) read the arguments
    after them up to their terminator as that command's whole argv. The
    batch terminator (+, only in its valid form immediately after {})
    belongs to -exec and -execdir alone: -ok and -okdir prompt for the
    command once per file, find itself rejects the {} + form for them,
    and a {} + inside one is an ordinary payload argument, so their
    span runs to the ;. A + anywhere else is an ordinary argument of
    the payload command, so the span runs on to the ; or a valid {} +.
    A span without its terminator runs to the end of the template: find
    would refuse the command anyway, and the interior is still checked.
    The data-taking predicates each read one argument of their own, the
    data actions read the counts their table names, and the
    operand-reading options (-files0-from, -regextype) read one argument
    of their own too, so the walk consumes those arguments with their
    token: an operand that looks like a payload option (-name -exec,
    -printf -exec, -files0-from -exec) is that data, and the span the
    payload really runs starts at the payload option behind it."""
    index = 1
    while index < len(args) - 1:
        data_arity = _find_data_arity(args[index])
        if data_arity is not None:
            index += 1 + data_arity
            continue
        if args[index].lower() not in ('-exec', '-execdir', '-ok', '-okdir'):
            index += 1
            continue
        # The {} + batch terminator is the -exec and -execdir form alone:
        # -ok and -okdir prompt for the command once per file and find
        # itself rejects the {} + form for them, so a {} + inside their
        # payload is an ordinary argument of the command they prompt for
        # and the span runs on to the ;.
        batch = args[index].lower() in ('-exec', '-execdir')
        end = index + 1
        while end < len(args) and not (args[end] == ';' or
                                       (batch and args[end] == '+' and args[end - 1] == '{}')):
            end += 1
        yield index, end
        index = end + 1


def _wrapper_option_names(token, values, optional_values):
    """The option names a wrapper spelling resolves to, and whether it
    ends awaiting its separate value.

    GNU option parsing lets a long option carry its value in its own token
    behind a separator (--cpu-list=0) and a short option carry it glued to
    its letter (-c0), and it bundles short options into one token, where
    every letter that takes no value stands on its own and the first letter
    that takes one ends the cluster, reading the rest of the token as its
    attached value (-punobody is -p and -u with nobody behind it). Short
    options keep their case the way the GNU parser reads them, so an
    uppercase letter spells the option of its own and never the lowercase
    one beside it (xargs's -P max-procs, sudo's -C close-from), and an
    option whose value attaches to its spelling only (xargs's -i, -l and
    -e, whose replace string, line count and eof string ride behind their
    letters or are not there at all) never awaits a separate value: a
    bare spelling reads nothing behind it. A spelling a walk reads resolves
    to the names the wrapper's tables hold, the long spelling split on its
    separator and its name resolved the way GNU option parsing resolves a
    long name, the exact name first and, failing that, an abbreviation
    exactly one documented long value spelling begins with (--max-pr is
    xargs's --max-procs) while a prefix more than one entry matches stays
    unresolved, and the short spelling read letter by letter until one of
    the letters takes a value, and the spelling ends awaiting that value
    when it ends at a separate-value letter with nothing behind it: a
    value rides with the token, so the walk consumes the token alone or
    only the argument behind it, and the resolved names keep their
    options' rules, the value the last one reads and the positional
    operand a resolved name removes."""
    if token.startswith('--'):
        name, separator, _value = token[2:].partition('=')
        name = '--' + name.lower()
        if name not in values and len(name) > 2:
            matches = [candidate for candidate in values
                       if candidate.startswith(name)]
            if len(matches) == 1:
                name = matches[0]
        return (name,), separator == '' and name in values
    body = token[1:]
    names = []
    for index, letter in enumerate(body):
        name = '-' + letter
        names.append(name)
        if name in values:
            return tuple(names), index + 1 == len(body)
        if name in optional_values:
            return tuple(names), False
    return tuple(names), False


def _wrapper_child_start(args, runner):
    """The index the program a wrapper runs starts at, or None.

    The wrapper reads its own options, with the values and the positional
    operands of _WRAPPER_SPECS, before the program it runs, so the first
    word left over is that program and everything behind it is the child
    argv: the same boundary the wrapper walk in _validate_template draws
    for the scan it makes with the child's own program facts. A -- the
    wrapper has not read as a value ends its own options, so the words
    behind it fill the remaining positional operands and the program
    starts among them; an option-looking word there is a word of the
    command, and a -- right behind an option waiting for its value stays
    that value the way a GNU parser reads it.
    """
    values, operands, drop_operand_after = _WRAPPER_SPECS[runner]
    optional_values = _WRAPPER_OPTIONAL_VALUES.get(runner, ())
    operands_left = 1 if operands == 'numeric' else operands
    awaiting_value = False
    separator_seen = False
    for index, arg in enumerate(args[1:], 1):
        if awaiting_value:
            awaiting_value = False
            continue
        if not separator_seen and arg == '--':
            separator_seen = True
            continue
        if not separator_seen and arg.startswith('-'):
            names, awaiting = _wrapper_option_names(arg, values, optional_values)
            if any(name in drop_operand_after for name in names):
                operands_left = 0
            awaiting_value = awaiting
            continue
        if operands_left > 0 and (operands != 'numeric' or _NUMERIC_OPERAND.match(arg)):
            operands_left -= 1
            continue
        return index
    return None


def _validate_find_template(args, windows):
    """-exec and its relatives run a command find builds itself, so the
    command after one must be fixed the way a wrapper's program is.

    The payload-taking options are the find family's own: -exec and -execdir
    build and run the command, -ok and -okdir prompt for it first, and each
    reads the arguments after it up to its terminator as that command's
    whole argv (_find_payload_spans draws each span). Every other
    carrier option reads a single value, a script, a command line or a
    program name, which the cross-program scan refuses a placeholder in as
    it is. Each payload span is validated as a nested command, with the same
    machinery the top level gets: the fixed-program check, the shell and
    interpreter family rules, and the carrier-name grammar, so a placeholder
    in the payload's program position, after a payload-interior shell's code
    option, or in an interior carrier option's value is refused. Payloads
    nest, because the nested command may itself be find with an -exec of its
    own, and the recursion carries the same rules to every depth. A fixed
    payload program keeps whole-operand placeholder arguments, which are
    data to the program it runs."""
    for start, end in _find_payload_spans(args):
        payload = args[start + 1:end]
        if payload and _PLACEHOLDER.search(payload[0]):
            raise ValueError('The command find runs after -exec must be a fixed program; pass the '
                             'placeholders to it as arguments')
        if payload and any(_PLACEHOLDER.search(arg) for arg in payload[1:]):
            _validate_template(payload, windows=windows)
            _reject_metadata_in_executing_options(payload)


def _long_value_less(name, names):
    """Whether a long option spelling resolves to a value-less flag.

    GNU option parsing accepts unambiguous prefixes of long option names,
    and rsync negates its options with a --no- prefix, so a spelling that
    names one documented value-less long flag, by its full name
    (--archive), by an unambiguous abbreviation of exactly one entry
    (--arch), or through the --no- negation of either (--no-archive),
    reads no value behind it. A genuinely ambiguous prefix or a spelling
    the table does not resolve keeps the GNU-convention reading a bare --
    right behind it takes as the option's separate value.
    """
    if name in names or sum(candidate.startswith(name) for candidate in names) == 1:
        return True
    stripped = name.removeprefix('no-')
    return stripped != name and (stripped in names or
                                 sum(candidate.startswith(stripped)
                                     for candidate in names) == 1)


def _long_data_operand(name, data_names, valueless_names):
    """Whether a long option spelling resolves to a documented data option.

    GNU option parsing accepts unambiguous prefixes of long option names,
    so a spelling that names one documented data option, by its full name
    (--files-from) or by an abbreviation exactly one entry of the
    program's option vocabulary matches (--files-f), reads the element
    behind it as that option's operand. A spelling that names a
    documented value-less flag instead (--backup, the rsync flag, not
    --backup-dir abbreviated) reads nothing behind it, and a genuinely
    ambiguous prefix or a spelling the tables do not resolve keeps the
    operand unconsumed.
    """
    if name in data_names:
        return True
    if name in valueless_names:
        return False
    matches = [candidate for candidate in (*valueless_names, *data_names)
               if candidate.startswith(name)]
    return len(matches) == 1 and matches[0] in data_names


def _reject_metadata_in_executing_options(args):
    """Refuse a placeholder in the value of an option that can carry code.

    The per-family rules keep metadata out of the program and code positions
    of the interpreters they model, but any other program's options are
    plain data in that model, and some options are executed whichever
    program takes them: sed runs the script --expression gives it, GNU tar
    runs its --checkpoint-action exec action, and a program outside the
    tables cannot be told from them by name alone. The names in
    _EXECUTING_OPTION_NAMES are matched for every program, with the value
    attached (--expression={{...}}) and with the operand the option keeps
    reading as its payload (-e {{...}}), so the whole argv is checked after
    the per-family rules, which keeps their own refusals and messages. An
    option named like a carrier is refused even when a custom tool means it
    as plain data (--file={{episode}}): telling the two apart needs
    per-program knowledge a blacklist keeps leaking, so the grammar stays
    strict for now. Where a program documents a reading of its own, the
    documented one wins over the cross-program match: the letters in
    _PROGRAM_SHORT_ARITY carry their program's real arity, so rsync's
    value-less short flags, -E and -F among the set, are not the -e/-f
    carriers their lowercase spells and tar's -I is the
    --use-compress-program carrier, and the documented data letters and
    names, rsync's -T and -f and the names in
    _PROGRAM_DATA_OPERAND_NAMES, read a plain data operand, which is
    consumed as data the way a carrier's own value is and never
    carrier-scanned, by the full spelling and by an unambiguous
    abbreviation of exactly one entry of the program's documented
    vocabulary the way the value-less attribution resolves one, so an
    abbreviated data-option spelling keeps its operand consumed while a
    spelling that names a value-less flag, more than one entry or nothing
    the tables carry leaves the operand to the scan. find's data
    predicates, data actions and operand-reading options (-files0-from
    among them) read their operands as plain data the same way, by the
    arity table the span walk shares, so the scan consumes the operand
    with its token instead of reading the predicate as a short bundle: the
    -name pattern is data to find, and a carrier-looking operand behind
    one stays open. A documented reading of a long name resolves ahead of
    the carrier prefix: an exact documented data option and the
    abbreviation exactly one documented entry resolves to are that data
    option (rsync's --info flags, tar's --so its --sort), and a
    documented value-less flag reads no value at all, so neither is the
    carrier whose name the spelling prefixes (tar's --checkpoint is not
    --checkpoint-action). A spelling the tables resolve to nothing keeps the
    prefix rule: a proper prefix of a carrier name is refused as the
    carrier a GNU parser could resolve it to (--checkpoint-act is
    --checkpoint-action to tar), a longer name a carrier's name happens to
    prefix is not, and a placeholder in the name part of an option is
    refused the way a placeholder value is, because the rendering can turn
    it into a carrier's attached form. An unconsumed -- ends option parsing
    for this argv, so the tokens after it are data operands, unless it is
    the separate value an option reads: a carrier keeps the argument it
    reads as its payload wherever the -- lands relative to it, and by the
    GNU convention a bare -- right behind an option that has read no value
    yet, attached or separate, is that option's value, so the tokens behind
    it stay scanned as options. A documented value-less flag reads nothing
    behind it, and a cluster of documented flags (-av) is value-less as a
    whole, and a documented value-less long flag (--archive), its --no-
    negation (--no-archive) and an unambiguous abbreviation of one (--arch,
    a prefix only one entry of the value-less set matches) read nothing
    behind it the same way, so a bare -- right behind one is the
    separator, not a value the flags could take, while a genuinely
    ambiguous or unsupported spelling keeps the GNU-convention value
    reading; a cluster that also carries a carrier letter
    keeps the carrier reading, and the -- right behind it still feeds that
    carrier its separate value. A short option that carries its operand
    glued to its letter, the data of tar's -C (-C/media) or the value of a
    carrier (-essh), has already read its value, so a bare -- behind it is
    the separator, not a second value, and a letter the program's own
    tables leave without an entry reads the text behind it the same way
    (sort's -S1M size), its bare spelling keeping the GNU-convention
    reading that a bare -- behind it is its value. GNU tar reads a first argument
    that carries no dash as its old-style option block, a bundle of
    letters with the same arity the dashed spellings have: each letter
    that reads a value takes the argument behind the bundle in the
    letters' order (cfI out.tar prog is -c -f out.tar -I prog), so the
    block's I reads the program it runs the way the dashed -I does, and
    a placeholder in the block itself is refused the way an option name
    is. The argument
    a carrier reads as its separate value is never scanned as an option
    itself, so a -- behind that consumed value ends option parsing and the
    data operands after it stay open.
    """
    if not any(_PLACEHOLDER.search(arg) for arg in args):
        return
    program = _interpreter_family(ntpath.basename(args[0]).lower().removesuffix('.exe'))
    short_flags, short_carriers, short_data = _PROGRAM_SHORT_ARITY.get(program, ((), (), ()))
    documented_letters = short_flags + short_carriers + short_data
    data_operand_names = _PROGRAM_DATA_OPERAND_NAMES.get(program, ())
    long_valueless_names = _PROGRAM_LONG_VALUELESS_NAMES.get(program, ())
    # find's -exec family runs each span after it as one nested command of
    # its own, and the find rules scan every span as the nested argv it is,
    # with the payload program's own options, so this scan consumes the
    # spans instead of reading their interior with find's facts.
    payload_ends = dict(_find_payload_spans(args)) if program == 'find' else {}
    # A wrapper validated the child argv with the wrapped program's own
    # facts, so this scan consumes the child the same way and never rereads
    # its interior with the wrapper's.
    child_start = _wrapper_child_start(args, program) if program in _WRAPPERS else None
    separator_seen = False
    consumed_positions = set()
    glued_operand_positions = set()
    for index, arg in enumerate(args):
        if not index:
            continue
        if index in payload_ends:
            # The span a find -exec reads is the nested command find runs,
            # terminator included, and it is scanned as that command's own
            # argv, so the tokens after the terminator keep their reading at
            # find's own level. The boundary wins over the consumed skip
            # below even when an earlier reading consumed the payload
            # option token itself as its value (the e a bundle such as
            # find's -prune spells), because the span walk drew the span
            # from the payload option and the nested validation already
            # read the interior with the payload program's own facts,
            # which a rescan with find's would only distort.
            for span_index in range(index + 1, min(payload_ends[index] + 1, len(args))):
                consumed_positions.add(span_index)
            continue
        if index == child_start:
            # The child argv a wrapper validated is the command it runs,
            # scanned once with the wrapped program's own facts, so the
            # tokens behind it are consumed here and keep that reading.
            # The boundary wins over the consumed skip below even when an
            # earlier option read the child program token itself as its
            # value (watch's --exec flag names the command it runs),
            # because the wrapper walk drew the boundary from the child
            # program position and the child argv already got its own
            # scan with the child's facts.
            for span_index in range(index + 1, len(args)):
                consumed_positions.add(span_index)
            continue
        if index in consumed_positions:
            # The token a carrier or a data option read as its separate
            # value, and the whole span a find -exec runs as a nested
            # command, are the payload that was consumed, not options of
            # their own, so the scan skips them and whatever follows keeps
            # its own reading.
            continue
        if arg == '--':
            previous = args[index - 1] if index > 1 else None
            # A documented value-less flag, and a cluster of documented
            # letters, reads nothing behind it, and a documented value-less
            # long flag, its --no- negation and an unambiguous abbreviation
            # of one, read nothing behind it the same way, so the bare --
            # right behind one is the separator, not a value the flag would
            # take; an option the arity tables leave undocumented or that
            # resolves to more than one entry keeps the GNU reading below.
            value_less = False
            if previous is not None and len(previous) > 1 and previous.startswith('-'):
                if previous.startswith('--'):
                    value_less = ('=' not in previous
                                  and _long_value_less(previous[2:].lower(), long_valueless_names))
                else:
                    value_less = set(previous[1:]) <= set(short_flags)
            if (not separator_seen and previous is not None and previous.startswith('-')
                    and not value_less
                    and (index - 1) not in consumed_positions
                    and (index - 1) not in glued_operand_positions and '=' not in previous):
                # A bare -- right behind an option that has read no value,
                # attached or separate, is that option's value by the GNU
                # convention, so it does not end option parsing and the tokens
                # behind it stay scanned as options.
                consumed_positions.add(index)
            else:
                separator_seen = True
            continue
        if index == 1 and program == 'tar' and not arg.startswith('-'):
            # GNU tar accepts the old-style syntax too: the first
            # argument, undashed, is a bundle of option letters, and a
            # letter that reads a value takes the argument behind the
            # bundle in the letters' order (tar cfI out.tar prog is
            # -c -f out.tar -I prog), so the block is read with the arity
            # the dashed spellings have: a value-less letter reads
            # nothing, the block's carrier letters (I) and the
            # cross-program carrier letters (f) read the argument behind
            # the bundle as the value the carrier refuses a placeholder
            # in, and every other letter, the data letters and the
            # value-taking letters the tables leave to their generic
            # readings (b), reads one plain data argument. A placeholder
            # in the bundle itself would fill option letters, so it is
            # refused the way an option name is, and the arguments the
            # block consumes are its letters' values, not options of
            # their own, so the scan skips them and the arguments behind
            # them keep their reading.
            if _PLACEHOLDER.search(arg):
                raise ValueError('The name of an option must be fixed; the placeholder '
                                 'would fill it with subtitle and provider metadata. Pass '
                                 'the placeholders to the program as plain arguments')
            operand_index = index + 1
            for letter in arg:
                if letter in short_flags:
                    continue
                option = None
                if letter in short_carriers:
                    option = '-' + letter
                elif letter.lower() in _EXECUTING_OPTION_LETTERS and letter not in short_data:
                    option = '-' + letter.lower()
                if operand_index >= len(args):
                    break
                if option is not None and _PLACEHOLDER.search(args[operand_index]):
                    raise ValueError(f'The {option} option must be fixed; it can carry '
                                     f'executable code, and the placeholder would fill it '
                                     f'with subtitle and provider metadata. Pass the '
                                     f'placeholders to the program as plain arguments')
                consumed_positions.add(operand_index)
                operand_index += 1
            continue
        if separator_seen or not arg.startswith('-'):
            continue
        data_arity = _find_data_arity(arg) if program == 'find' else None
        if data_arity is not None:
            # find's data predicates and actions read their operands as
            # plain data (the -name pattern, the -printf format), so the
            # scan consumes the operand with its token by the same arity
            # the span walk uses instead of reading the predicate as a
            # short bundle: the operand is data to find, and a
            # carrier-looking token there stays open.
            for operand_index in range(index + 1, min(index + 1 + data_arity, len(args))):
                consumed_positions.add(operand_index)
            continue
        long_option = arg.startswith('--')
        body = arg[2:] if long_option else arg[1:]
        name, separator, value = body.partition('=')
        lowered = name.lower()
        option = None
        # A documented spelling of the program's own keeps its documented
        # reading ahead of the cross-program set, the way GNU option
        # parsing resolves a program's own name ahead of a foreign one: a
        # short letter the program documents keeps it (rsync's -E is a
        # flag, not the -e carrier its lowercase spells in the cross-program
        # set), and a long name the program's documented vocabulary
        # resolves, by its full name or by the abbreviation exactly one
        # entry of it resolves to, keeps it as well (rsync's --exec is its
        # --executability abbreviated, a flag reading nothing, not the exec
        # carrier the cross-program set matches), so the generic match
        # stands down for both and the readings below apply, keeping --rsh
        # behind --exec the live carrier it is.
        documented_data = False
        documented_flag = False
        if long_option:
            documented_data = _long_data_operand(lowered, data_operand_names,
                                                 long_valueless_names)
            documented_flag = _long_value_less(lowered, long_valueless_names)
        if lowered in _EXECUTING_OPTION_NAMES and not (
                (not long_option and name in documented_letters)
                or documented_data or documented_flag):
            option = ('--' if long_option else '-') + lowered
            payload = value if separator else None
        elif long_option:
            if _PLACEHOLDER.search(name):
                # The name is not fixed, so the position it fills cannot be
                # vouched for as data: the rendering can turn it into a
                # carrier's attached form.
                raise ValueError('The name of an option must be fixed; the placeholder would fill '
                                 'it with subtitle and provider metadata. Pass the placeholders to '
                                 'the program as plain arguments')
            # A documented reading of the name resolves before the
            # carrier-prefix fallback does, the way GNU option parsing
            # resolves an exact name ahead of a prefix: a documented data
            # option, by its full name or by the abbreviation exactly one
            # entry of the program's documented vocabulary resolves to, is
            # that data option (rsync's --info verbosity flags, tar's --so
            # the --sort it abbreviates), so its attached value is data and
            # its separate operand is consumed below, and a documented
            # value-less flag (tar's --checkpoint) reads no value at all.
            # Neither is the carrier whose name the spelling prefixes:
            # --info is not --info-script and --checkpoint is not
            # --checkpoint-action.
            if documented_data:
                if not separator and index + 1 < len(args):
                    # A data option the program documents reads its operand
                    # from the next argument (tar's --label the literal
                    # archive label), so that operand is consumed as data and
                    # never carrier-scanned, whatever it looks like, the way a
                    # carrier's own value is. The spelling resolves the way
                    # GNU option parsing resolves it, so an unambiguous
                    # abbreviation of the one data option it names (tar's
                    # --files-f, --files-from abbreviated) consumes its
                    # operand too, while a spelling that names a documented
                    # value-less flag instead (--backup the rsync flag, not
                    # --backup-dir abbreviated), a prefix more than one entry
                    # matches or a spelling the tables do not carry leaves
                    # the operand to the scan.
                    consumed_positions.add(index + 1)
            elif not documented_flag:
                # GNU option parsing accepts unambiguous prefixes of long
                # option names, so a proper prefix of a carrier name is the
                # carrier a GNU parser could resolve the token to, and it is
                # refused as that carrier. A proper prefix only: a longer
                # name a carrier's name happens to prefix belongs to some
                # other tool.
                carrier = next((candidate for candidate in sorted(_EXECUTING_OPTION_NAMES)
                                if lowered and len(candidate) > len(lowered)
                                and candidate.startswith(lowered)), None)
                if carrier is not None:
                    option = '--' + carrier
                    payload = value if separator else None
        else:
            # A short bundle is read the way the option parser reads it: the
            # letters in front are flags and the carrier letter takes the
            # rest of the token as its value, or the next argument when the
            # token ends at it (-ne, -ne'script', -fscript), and an equals
            # separator keeps that reading: the letters up to it are the
            # cluster and the text from it on is the carrier's value
            # (-ne={{...}}, where sed's -e receives the script ={{...}}),
            # so the value is refused with the separator as without it.
            # A placeholder at or in front of the carrier letter is in the
            # cluster, the option names themselves, so the rendering can
            # turn it into the carrier and the name is refused; behind the
            # carrier letter it is the attached value and keeps the value's
            # refusal. A bundle without a carrier letter is all names.
            # The program's own documented letters keep their documented
            # reading instead of the case-blind one: a value-less flag like
            # rsync's -E consumes nothing, a documented carrier like tar's
            # -I is the carrier its lowercase never spells, and a
            # documented data option like tar's -C or rsync's -f reads its
            # operand glued to the letter or from the next argument, so a
            # placeholder rides in as data.
            for position, letter in enumerate(name):
                if letter in short_flags:
                    continue
                if letter in short_carriers or (letter.lower() in _EXECUTING_OPTION_LETTERS
                                               and letter not in short_data):
                    if any(match.start() <= position for match in _PLACEHOLDER.finditer(name)):
                        raise ValueError('The name of an option must be fixed; the placeholder '
                                         'would fill it with subtitle and provider metadata. Pass '
                                         'the placeholders to the program as plain arguments')
                    option = '-' + (letter if letter in short_carriers else letter.lower())
                    payload = body[position + 1:] or None
                    if payload is not None:
                        # The carrier carries its value glued to its letter
                        # (-essh), so it has already read its value exactly
                        # as an equals-attached long option has, and the
                        # bare -- behind the token is the separator, not a
                        # second value the carrier would take.
                        glued_operand_positions.add(index)
                    break
                if letter in short_data:
                    if any(match.start() <= position for match in _PLACEHOLDER.finditer(name)):
                        raise ValueError('The name of an option must be fixed; the placeholder '
                                         'would fill it with subtitle and provider metadata. Pass '
                                         'the placeholders to the program as plain arguments')
                    if body[position + 1:]:
                        glued_operand_positions.add(index)
                    elif index + 1 < len(args):
                        consumed_positions.add(index + 1)
                    break
                if documented_letters:
                    # A letter the program's own tables leave without an
                    # entry is a value option the program documents (sort's
                    # -S size, -t separator character and -k key definition,
                    # tar's -b blocking factor, held apart from the data
                    # letters because an option-shaped word never parses as
                    # their value) or a letter outside its vocabulary, and
                    # either way the option parser gives the text behind the
                    # letter to the letter itself: the token has read its
                    # attached operand, so a bare -- behind it is the
                    # separator, not a second value, while the bare spelling
                    # (-S) keeps awaiting the separate value a GNU parser
                    # reads a -- behind it as. A placeholder at or in front
                    # of the letter is part of the option names and is
                    # refused the way a carrier's cluster is; behind the
                    # letter it is the attached value and rides as the data
                    # it is.
                    if any(match.start() <= position for match in _PLACEHOLDER.finditer(name)):
                        raise ValueError('The name of an option must be fixed; the placeholder '
                                         'would fill it with subtitle and provider metadata. Pass '
                                         'the placeholders to the program as plain arguments')
                    if body[position + 1:]:
                        glued_operand_positions.add(index)
                    break
            else:
                if _PLACEHOLDER.search(name):
                    raise ValueError('The name of an option must be fixed; the placeholder would '
                                     'fill it with subtitle and provider metadata. Pass the '
                                     'placeholders to the program as plain arguments')
        if option is None:
            continue
        if payload is None:
            if index + 1 >= len(args):
                continue
            payload = args[index + 1]
            # The option keeps reading as its payload whatever the argument
            # is, a -- included, so that argument was consumed here and does
            # not end option parsing when the scan reaches it.
            consumed_positions.add(index + 1)
        if _PLACEHOLDER.search(payload):
            raise ValueError(f'The {option} option must be fixed; it can carry executable code, '
                             f'and the placeholder would fill it with subtitle and provider '
                             f'metadata. Pass the placeholders to the program as plain arguments')


def _reject_watch_shell_metadata(child_argv, exec_mode_seen):
    """watch assembles the command line it is given into a shell command
    string in its default mode (its help: -x passes the command to exec
    instead of "sh -c"), so a placeholder in it rides into the shell text
    the wrapper rules refuse everywhere; -x and --exec name the exec mode,
    which hands the command line to execvp directly, so the placeholders
    stay the plain arguments of a fixed program."""
    if not exec_mode_seen and any(_PLACEHOLDER.search(arg) for arg in child_argv):
        raise ValueError('watch assembles its command line into shell command text without '
                         '-x or --exec; pass the placeholders with the exec mode, or keep the '
                         'command free of them')


def _validate_template(args, windows):
    if not args or not args[0] or _PLACEHOLDER.search(args[0]):
        raise ValueError('Post-processing requires a fixed executable path')
    program = ntpath.basename(args[0]).lower()
    if windows and program.endswith(('.bat', '.cmd')):
        raise ValueError('.bat and .cmd files are run through cmd.exe, which is a shell; use an executable, '
                         'or an interpreter with a script file, for post-processing')
    if windows and program in _WINDOWS_INTERNAL:
        raise ValueError(f'Commands run without a shell, so the cmd.exe command "{program}" is not a '
                         f'program; use an executable or an interpreter with a script file instead')
    if not windows and program in _POSIX_INTERNAL:
        raise ValueError(f'Commands run without a shell, so the shell built-in "{program}" is not a '
                         f'program; run a script directly, or an interpreter with a script file')
    if not any(_PLACEHOLDER.search(arg) for arg in args[1:]):
        return
    runner = _interpreter_family(program.removesuffix('.exe'))
    if runner in _WRAPPERS:
        # Check the program a wrapper runs as if it came first. A placeholder
        # before any fixed program would let subtitle metadata choose what the
        # wrapper executes, so it is refused. The wrapper's own option values
        # and positional operands (sudo's user, timeout's duration) are data
        # it consumes, not the program, so they never satisfy that check.
        # The first word left over is the program the wrapper runs and the
        # words behind it are that program's own argv, so only the first
        # gets the nested validation: a later operand that names a wrapper
        # or a shell of its own is an operand the child consumes, never a
        # program the wrapper runs, and the validation stays with the real
        # child's own options.
        values, operands, drop_operand_after = _WRAPPER_SPECS[runner]
        optional_values = _WRAPPER_OPTIONAL_VALUES.get(runner, ())
        operands_left = 1 if operands == 'numeric' else operands
        fixed_program_seen = False
        child_index = None
        awaiting_value = False
        exec_mode_seen = False
        separator_seen = False
        index = 1
        while index < len(args):
            arg = args[index]
            if _PLACEHOLDER.search(arg):
                if not fixed_program_seen:
                    raise ValueError('A wrapper such as nice or sudo must run a fixed program; pass the '
                                     'placeholders to that program as arguments')
                break
            if awaiting_value:
                awaiting_value = False
                index += 1
                continue
            if not separator_seen and not fixed_program_seen and arg == '--':
                # A -- the wrapper has not read as a value ends its own
                # options: the words behind it are the command's, so the
                # walk reads no option and no mode of its own there, an
                # option-looking word among them is a word of the command
                # (watch's -x behind the separator is shell text, not the
                # exec mode), and the remaining positional operands still
                # consume before the program starts among them.
                separator_seen = True
                index += 1
                continue
            name = _interpreter_family(ntpath.basename(arg).lower().removesuffix('.exe'))
            if not separator_seen and not fixed_program_seen and arg.startswith('-'):
                names, awaiting = _wrapper_option_names(arg, values, optional_values)
                # watch's -x and --exec name the exec mode that hands the
                # command line to execvp directly, and --ex is the
                # abbreviation the installed procps accepts; a shorter
                # prefix stays ambiguous in the real tool.
                if runner == 'watch' and any(name in ('-x', '--exec', '--ex') for name in names):
                    exec_mode_seen = True
                if any(name in drop_operand_after for name in names):
                    operands_left = 0
                awaiting_value = awaiting
                index += 1
                continue
            if not fixed_program_seen and operands_left > 0 and (
                    operands != 'numeric' or _NUMERIC_OPERAND.match(arg)):
                operands_left -= 1
                index += 1
                continue
            if separator_seen or not arg.startswith('-'):
                fixed_program_seen = True
                if child_index is None:
                    child_index = index
            if index == child_index and (
                    name in _WRAPPERS or name in _SHELLS or name.startswith('python')
                    or name in ('env', 'command', 'exec', 'cmd', 'powershell', 'pwsh',
                                'node', 'nodejs', 'perl', 'ruby', 'php')
                    or name in _POSITIONAL_CODE or name == 'find'):
                _validate_template(args[index:], windows)
                _reject_metadata_in_executing_options(args[index:])
                if runner == 'watch':
                    _reject_watch_shell_metadata(args[index:], exec_mode_seen)
                return
            index += 1
        # The wrapper consumes its own -- as the end of its options, so the
        # child argv begins after it with no separator of its own; the
        # whole-argv scan stops at the wrapper's -- and would leave the
        # child's executable options unchecked, so each child argv gets the
        # carrier scan with its own separator state.
        if child_index is not None:
            _reject_metadata_in_executing_options(args[child_index:])
            if runner == 'watch':
                _reject_watch_shell_metadata(args[child_index:], exec_mode_seen)
        return
    if runner in ('env', 'command', 'exec'):
        raise ValueError('Pass subtitle metadata to a direct executable or a fixed script file')
    if runner == 'cmd':
        raise ValueError('Pass subtitle metadata to a script file, not cmd command text')
    if runner in ('powershell', 'pwsh'):
        if any(arg.lower() in ('-command', '-commandwithargs', '-encodedcommand', '-ec', '-c')
               for arg in args[1:]):
            raise ValueError('Pass subtitle metadata to a fixed PowerShell script file')
        _validate_interpreter_template(
            args, code_options=('-command', '-commandwithargs', '-encodedcommand', '-ec', '-c'),
            script_options=('-file', '-f'), value_options=('-executionpolicy',),
            simple_options=('-noprofile', '-noninteractive', '-nologo'),
            placeholders_after_code=False)
    elif runner in _SHELLS:
        _validate_interpreter_template(
            args, value_options=('--rcfile', '--init-file', '-o', '-O'),
            simple_options=('-e', '-u', '-f', '-x', '-v', '-n', '-l'),
            shell_short_options=True)
    elif runner.startswith('python'):
        # Everything after python's -c code is plain sys.argv, so metadata
        # can safely follow the code text here.
        _validate_interpreter_template(
            args, code_options=('-c',), script_options=('-m',),
            value_options=('-X', '-W', '--check-hash-based-pycs'),
            simple_options=('-I', '-B', '-E', '-s', '-S', '-u', '-O', '-OO', '-q', '-v'),
            code_texts={'-c': 'python -c code'})
    elif runner in ('node', 'nodejs'):
        _validate_interpreter_template(
            args, code_options=('-e', '--eval'),
            value_options=('-r', '--require', '--import', '--loader', '--conditions'),
            simple_options=('--no-warnings', '--trace-warnings'),
            code_texts={'-e': 'node -e code', '--eval': 'node --eval code'},
            placeholders_after_code=False)
    elif runner == 'perl':
        _validate_interpreter_template(
            args, code_options=('-e', '-E'), value_options=('-I', '-M', '-m'),
            simple_options=('-w', '-T'),
            code_texts={'-e': 'perl -e code', '-E': 'perl -E code'},
            placeholders_after_code=False)
    elif runner == 'ruby':
        _validate_interpreter_template(
            args, code_options=('-e',), value_options=('-I', '-r'), simple_options=('-w',),
            code_texts={'-e': 'ruby -e code'},
            placeholders_after_code=False)
    elif runner == 'php':
        _validate_interpreter_template(
            args, code_options=('-r',), value_options=('-d', '-c'), simple_options=('-n', '-q'),
            code_texts={'-r': 'php -r code'},
            placeholders_after_code=False)
    elif runner in _POSITIONAL_CODE:
        _validate_positional_code_template(args, *_POSITIONAL_CODE[runner])
    elif runner == 'find':
        _validate_find_template(args, windows)


def parse_postprocessing_command(command, windows=None):
    """Parse a configured post-processing template into an argument list.

    Raises ValueError, with a message fit for the user, when the template relies
    on a shell, lets subtitle metadata choose what runs, or puts it in an option
    whose value the program executes."""
    if windows is None:
        windows = os.name == 'nt'
    _reject_shell_syntax(command, windows)
    args = _split_command(command, windows=windows)
    _validate_template(args, windows=windows)
    _reject_metadata_in_executing_options(args)
    return args


def pp_replace(pp_command, episode, subtitles, language, language_code2, language_code3, episode_language,
               episode_language_code2, episode_language_code3, score, subtitle_id, provider, uploader,
               release_info, series_id, episode_id):
    args = parse_postprocessing_command(pp_command)
    values = {
        'directory': os.path.dirname(episode),
        'episode': episode,
        'episode_name': os.path.splitext(os.path.basename(episode))[0],
        'subtitles': subtitles,
        'subtitles_language': language,
        'subtitles_language_code2': language_code2,
        'subtitles_language_code3': language_code3,
        'subtitles_language_code2_dot': str(language_code2).replace(':', '.'),
        'subtitles_language_code3_dot': str(language_code3).replace(':', '.'),
        'episode_language': episode_language,
        'episode_language_code2': episode_language_code2,
        'episode_language_code3': episode_language_code3,
        'score': score,
        'subtitle_id': subtitle_id,
        'provider': provider,
        'uploader': uploader,
        'release_info': release_info,
        'series_id': series_id,
        'episode_id': episode_id,
    }
    # One substitution pass: values containing placeholders remain literal. A
    # value the caller does not have (a series id on a movie, an uploader or
    # a release the provider never named) arrives as None and becomes an
    # empty argument, not the string 'None' a plain str() would hand the
    # script as if it were real metadata.
    def _argument(match):
        key = match[1]
        if key not in values:
            return match[0]
        value = values[key]
        return '' if value is None else str(value)

    return [_PLACEHOLDER.sub(_argument, arg) for arg in args]


def set_chmod(subtitles_path):
    # apply chmod if required
    chmod = int(settings.general.chmod, 8) if not sys.platform.startswith(
        'win') and settings.general.chmod_enabled else None
    if chmod:
        logging.debug(f"BAZARR setting permission to {chmod} on {subtitles_path} after custom post-processing.")  # noqa: G004
        os.chmod(subtitles_path, chmod)
