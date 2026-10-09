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
_POSIX_EXPANSION = re.compile(r'\$[({\w@#?$!*0-9-]')
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
    'sudo': (('-u', '-g', '-p', '-r', '-t', '-C', '-D', '-R', '-T', '-S'), 0, ()),
    'doas': (('-u', '-a', '-C', '-L'), 0, ()),
    'ionice': (('-c', '-n', '-t'), 0, ()),
    'stdbuf': (('-o', '-e', '-i'), 0, ()),
    'setsid': ((), 0, ()),
    'xargs': (('-I', '-i', '-L', '-l', '-n', '-P', '-s', '-E', '-e', '-a', '--arg-file',
               '-d', '--delimiter'), 0, ()),
    'flock': (('-w', '--wait', '-W', '-E', '--conflict-exit-code', '-c', '--command'), 1, ()),
    'busybox': ((), 0, ()),
    'chrt': ((), 1, ()),
    'taskset': (('-c', '--cpu-list'), 1, ('-c', '--cpu-list')),
    'runuser': (('-u', '--user', '-g', '--group', '-G', '--supp-group', '-c', '--command',
                 '--session-command'), 1, ('-u', '--user')),
    'gosu': ((), 1, ()),
    'su-exec': ((), 1, ()),
    's6-setuidgid': ((), 1, ()),
        # watch: only -n takes a value; -e, -x and --exec are flags, so the
    # program sits in the program position behind them.
    'watch': (('-n', '--interval'), 0, ()),
    # su: the text after -c and --command runs through the target account's
    # shell, so a placeholder there is shell code. The wrapper rules refuse
    # one before any fixed program appears, which covers the -c operand with
    # and without -s naming that shell; -s only selects it.
    'su': (('-c', '--command', '--session-command', '-s', '--shell', '-g', '--group',
            '-G', '--supp-group', '-w', '--whitelist-environment'), 1, ()),
}
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


def _validate_find_template(args):
    """-exec and its relatives run a command find builds itself, so the command
    after one must be fixed the way a wrapper's program is."""
    for index in range(1, len(args) - 1):
        if args[index].lower() in ('-exec', '-execdir', '-ok', '-okdir') \
                and _PLACEHOLDER.search(args[index + 1]):
            raise ValueError('The command find runs after -exec must be a fixed program; pass the '
                             'placeholders to it as arguments')


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
        values, operands, drop_operand_after = _WRAPPER_SPECS[runner]
        operands_left = 1 if operands == 'numeric' else operands
        fixed_program_seen = False
        awaiting_value = False
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
            name = _interpreter_family(ntpath.basename(arg).lower().removesuffix('.exe'))
            if not fixed_program_seen and arg.startswith('-'):
                lower = arg.lower()
                if lower in drop_operand_after:
                    operands_left = 0
                if lower in values:
                    awaiting_value = True
                index += 1
                continue
            if not fixed_program_seen and operands_left > 0 and (
                    operands != 'numeric' or _NUMERIC_OPERAND.match(arg)):
                operands_left -= 1
                index += 1
                continue
            if not arg.startswith('-'):
                fixed_program_seen = True
            if (name in _WRAPPERS or name in _SHELLS or name.startswith('python')
                    or name in ('env', 'command', 'exec', 'cmd', 'powershell', 'pwsh', 'node', 'nodejs',
                                'perl', 'ruby', 'php')
                    or name in _POSITIONAL_CODE or name == 'find'):
                _validate_template(args[index:], windows)
                return
            index += 1
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
        _validate_find_template(args)


def parse_postprocessing_command(command, windows=None):
    """Parse a configured post-processing template into an argument list.

    Raises ValueError, with a message fit for the user, when the template relies
    on a shell or lets subtitle metadata choose what runs."""
    if windows is None:
        windows = os.name == 'nt'
    _reject_shell_syntax(command, windows)
    args = _split_command(command, windows=windows)
    _validate_template(args, windows=windows)
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
