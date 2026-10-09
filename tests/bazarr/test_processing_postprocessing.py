# coding=utf-8
# Per-instance post-processing resolution (#227).
# The helper resolves the six post-processing settings against the owning
# Sonarr/Radarr instance's overrides, falling back to the global config.
import json
import re
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from dynaconf.validator import ValidationError

from app.config import settings
from arr_instances import resolution
from subtitles.post_processing import _postprocessing_locked
from subtitles.processing import _postprocessing_config
from utilities.post_processing import parse_postprocessing_command, pp_replace
from test_sportarr_kind_migration import migration_engine  # noqa: F401


@pytest.fixture
def seed_instance_settings():
    def _seed(blob, instance_id=7777):
        resolution._subtitle_settings_cache[instance_id] = blob
        return instance_id

    yield _seed
    resolution.clear_subtitle_settings_cache()


def test_global_when_no_instance(monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing", True)
    monkeypatch.setattr(settings.general, "postprocessing_cmd", "/global.sh")
    monkeypatch.setattr(settings.general, "use_postprocessing_threshold", True)
    monkeypatch.setattr(settings.general, "postprocessing_threshold", 80)
    assert _postprocessing_config("series", None) == (True, "/global.sh", True, 80)


def test_instance_override_series(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing", False)
    monkeypatch.setattr(settings.general, "postprocessing_cmd", "/global.sh")
    iid = seed_instance_settings({"general": {
        "use_postprocessing": True,
        "postprocessing_cmd": "/instance.sh",
        "use_postprocessing_threshold": True,
        "postprocessing_threshold": 60,
    }})
    assert _postprocessing_config("series", iid) == (True, "/instance.sh", True, 60)


def test_instance_movie_uses_movie_threshold(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing", True)
    monkeypatch.setattr(settings.general, "postprocessing_cmd", "/global.sh")
    iid = seed_instance_settings({"general": {
        "use_postprocessing_threshold_movie": True,
        "postprocessing_threshold_movie": 45,
    }})
    use_pp, cmd, use_th, th = _postprocessing_config("movie", iid)
    assert use_th is True and th == 45


def test_instance_without_threshold_override_falls_back_to_global(seed_instance_settings, monkeypatch):
    monkeypatch.setattr(settings.general, "use_postprocessing_threshold", True)
    monkeypatch.setattr(settings.general, "postprocessing_threshold", 75)
    iid = seed_instance_settings({"general": {"use_postprocessing": True}})
    use_pp, cmd, use_th, th = _postprocessing_config("series", iid)
    assert use_pp is True and use_th is True and th == 75


def test_threshold_coerced_to_int(seed_instance_settings, monkeypatch):
    # the global value may be stored as a string; the helper must return an int
    monkeypatch.setattr(settings.general, "postprocessing_threshold", "90")
    iid = seed_instance_settings({"general": {}})
    _, _, _, th = _postprocessing_config("series", iid)
    assert th == 90 and isinstance(th, int)


# Configured commands are parsed into an argument list before any subtitle
# metadata is inserted, and run without a shell.


def render(command, *, episode='/media/a show.mkv', subtitles='/media/a show.en.srt',
           language='English', episode_language='English', provider='provider',
           release_info='release', series_id=12, episode_id=34):
    return pp_replace(
        command, episode, subtitles, language, 'en', 'eng', episode_language,
        'en', 'eng', 91, 'subtitle-id', provider, 'uploader', release_info,
        series_id, episode_id,
    )


def test_posix_template_preserves_quoted_program_and_inline_arguments():
    assert render('"/opt/my tools/process" --input={{episode}} '
                  '"prefix {{subtitles}} suffix" --score={{score}}') == [
        '/opt/my tools/process', '--input=/media/a show.mkv',
        'prefix /media/a show.en.srt suffix', '--score=91',
    ]


def test_empty_values_and_explicit_empty_argument_keep_their_positions():
    assert render('process "{{provider}}" "" --audio={{episode_language}}',
                  provider='', episode_language='') == [
        'process', '', '', '--audio=',
    ]


def test_values_the_caller_does_not_have_become_empty_arguments():
    """A series id on a movie, or an uploader or a release the provider never named,
    arrives as None. It becomes an empty argument, not the string 'None' a script
    would take for real metadata."""
    assert render('process --series={{series_id}} --release={{release_info}} "{{subtitles}}"',
                  series_id=None, release_info=None) == [
        'process', '--series=', '--release=', '/media/a show.en.srt',
    ]


def test_unknown_placeholders_remain_literal_and_values_are_not_expanded_again():
    assert render('process {{unknown}} --release={{release_info}} {{episode}}',
                  release_info='{{episode}}') == [
        'process', '{{unknown}}', '--release={{episode}}', '/media/a show.mkv',
    ]


@pytest.mark.parametrize('payload', [
    '; touch /tmp/never', '$(touch /tmp/never)', '`touch /tmp/never`',
    '& calc.exe', '| tee /tmp/never', '"; echo escaped', "it's", 'line one\nline two',
    '{{episode}}; $(id)', r'C:\Media\literal\name',
])
def test_untrusted_metadata_stays_in_one_argument(payload):
    assert render('process --release={{release_info}}', release_info=payload) == [
        'process', '--release=' + payload,
    ]


def test_argv_reaches_a_real_child_unchanged(tmp_path):
    helper_dir = tmp_path / 'scripts with spaces'
    helper_dir.mkdir()
    helper = helper_dir / 'capture.py'
    output = tmp_path / 'arguments.json'
    helper.write_text(
        'import json, sys\n'
        'with open(sys.argv[1], "w", encoding="utf-8") as f:\n'
        '    json.dump(sys.argv[2:], f)\n',
        encoding='utf-8',
    )
    marker = tmp_path / 'never'
    payload = (f'literal; touch {marker} $(touch {marker}) `touch {marker}` & | '
               f'"double" \'single\'\nsecond line {{{{episode}}}}')
    episode = str(tmp_path / 'a show.mkv')
    command = ' '.join(shlex.quote(part) for part in
                       (sys.executable, str(helper), str(output)))
    # An empty placeholder still produces an argument, so two empty values
    # reach the child as argv ["", ""], unlike the old shell string.
    argv = render(command + ' --input={{episode}} "{{release_info}}" "Name: {{release_info}}" '
                         '"{{series_id}}" "{{episode_id}}"',
                  episode=episode, release_info=payload, series_id='', episode_id='')

    _postprocessing_locked(argv, episode)

    assert json.loads(output.read_text(encoding='utf-8')) == [
        '--input=' + episode, payload, 'Name: ' + payload, '', '',
    ]
    assert not marker.exists()


@pytest.mark.parametrize('source, expected', [
    (r'"C:\Program Files\Python\python.exe" "C:\Media\My Show\episode.mkv"',
     [r'C:\Program Files\Python\python.exe', r'C:\Media\My Show\episode.mkv']),
    (r'process "\\Server\share\my dir\sub.srt"',
     ['process', r'\\Server\share\my dir\sub.srt']),
    (r'process "C:\Media\my dir\\"',
     ['process', 'C:\\Media\\my dir\\']),
    (r'process "say \"hello\""', ['process', 'say "hello"']),
    (r'process ""', ['process', '']),
    (r'C:\tools\process.exe "a | b" {{subtitles}}',
     [r'C:\tools\process.exe', 'a | b', '{{subtitles}}']),
    # cmd.exe never treated single quotes as quoting, but a single-quoted placeholder used to work.
    (r"C:\tools\process.exe '{{subtitles}}' '{{directory}}\{{episode_name}}.srt' 'literal'",
     [r'C:\tools\process.exe', '{{subtitles}}', r'{{directory}}\{{episode_name}}.srt', "'literal'"]),
])
def test_windows_template_parser_uses_windows_quote_and_backslash_rules(source, expected):
    assert parse_postprocessing_command(source, windows=True) == expected


def test_windows_parser_roundtrips_python_windows_argv_serialization():
    from utilities.post_processing import _split_command
    expected = [r'C:\Program Files\python.exe', '\\\\Server\\media share\\',
                'quote " inside', '', 'C:\\trailing\\']
    assert _split_command(subprocess.list2cmdline(expected), windows=True) == expected


def test_windows_template_inserts_metadata_without_cmd_quoting():
    with mock.patch('utilities.post_processing.os.name', 'nt'):
        assert render(r'"C:\Program Files\tool.exe" "{{release_info}}" {{episode}}',
                      episode=r'C:\Media\My Show\a.mkv', release_info='a&calc') == [
            r'C:\Program Files\tool.exe', 'a&calc', r'C:\Media\My Show\a.mkv',
        ]


@pytest.mark.parametrize('command', [
    'process {{subtitles}} | tee /tmp/log',
    'process {{subtitles}} && other',
    'process {{subtitles}} || other',
    'process {{subtitles}}; other',
    'process {{subtitles}} > /tmp/log',
    'process {{subtitles}} 2>&1',
    'process < /tmp/input {{subtitles}}',
    'process {{subtitles}} &',
    'process $HOME/{{subtitles}}',
    'process "${HOME}/{{subtitles}}"',
    'process "$(id)" {{subtitles}}',
    'process `id` {{subtitles}}',
    '~/process {{subtitles}}',
    'process {{subtitles}}\nother',
    'process {{subtitles}}\\\ncontinue',
    'chmod 644 {{directory}}/*.srt',
    'LANG=C /opt/process {{subtitles}}',
    '/opt/process {{subtitles}} # note',
    '(process "{{subtitles}}")',
    '( process "{{subtitles}}" )',
    '/opt/process "$@" {{subtitles}}',
    '/opt/process {{directory}}/subtitle?.srt',
    '/opt/process [abc].srt {{subtitles}}',
])
def test_posix_shell_syntax_is_refused(command):
    with pytest.raises(ValueError, match='without a shell'):
        parse_postprocessing_command(command, windows=False)


@pytest.mark.parametrize('command', [
    r'C:\tools\process.exe {{subtitles}} | more',
    r'C:\tools\process.exe {{subtitles}} && other',
    r'C:\tools\process.exe {{subtitles}} > C:\log.txt',
    r'C:\tools\process.exe {{subtitles}} ^& other',
    r'C:\tools\process.exe "%TEMP%\x" {{subtitles}}',
    r'(C:\tools\process.exe "{{subtitles}}")',
])
def test_windows_cmd_syntax_is_refused(command):
    with pytest.raises(ValueError, match='without a shell'):
        parse_postprocessing_command(command, windows=True)


# Every refusal class is named in the message a stored bad template raises,
# which is the text the health issue and the save-time rejection carry.
@pytest.mark.parametrize('command, windows, named', [
    ('process {{subtitles}} | tee /tmp/log', False, 'pipes'),
    ('process {{subtitles}} && other', False, '&&'),
    ('process $HOME/{{subtitles}}', False, 'variables'),
    ('sh -c "echo {{release_info}}"', False, 'sh -c'),
    ('python -c "print({{release_info}})"', False, 'python -c'),
    ('nice {{subtitles}}', False, 'nice or sudo'),
    ('process.bat "{{subtitles}}"', True, '.bat and .cmd'),
    (r'C:\tools\process.exe {{subtitles}} | more', True, 'pipes'),
    (r'C:\tools\process.exe "%TEMP%\x" {{subtitles}}', True, '%TEMP%'),
    ('node -e "console.log({{release_info}})"', False, 'node -e'),
    ('node --eval "console.log({{release_info}})"', False, 'node --eval'),
    ('nodejs -e "console.log({{release_info}})"', False, 'node -e'),
    ('perl -e "print {{release_info}}"', False, 'perl -e'),
    ('perl -E "say {{release_info}}"', False, 'perl -E'),
    ('ruby -e "puts {{release_info}}"', False, 'ruby -e'),
    ('php -r "echo {{release_info}};"', False, 'php -r'),
    ('ash -c "echo {{release_info}}"', False, 'sh -c'),
    ('tcsh -c "echo {{release_info}}"', False, 'sh -c'),
    ('(process "{{subtitles}}")', False, "'('"),
    ('/opt/process "$@" {{subtitles}}', False, "'$'"),
    ('/opt/process {{directory}}/subtitle?.srt', False, "'?'"),
    ('process {{subtitles}}\\\ncontinue', False, 'joined the lines'),
])
def test_every_refusal_class_is_named_in_the_message(command, windows, named):
    with pytest.raises(ValueError, match=re.escape(named)):
        parse_postprocessing_command(command, windows=windows)


def test_quoted_or_escaped_shell_characters_stay_literal():
    assert parse_postprocessing_command(
        'process "a | b" \'c && $d\' e\\;f "~" "{{subtitles}}" "*.srt" a#b --lang=C', windows=False) == [
        'process', 'a | b', 'c && $d', 'e;f', '~', '{{subtitles}}', '*.srt', 'a#b', '--lang=C',
    ]


def test_parentheses_inside_a_word_stay_literal():
    """An unquoted parenthesis at a word edge is the grouping syntax a shell used
    to read; one inside a word is just a character, the way it was when quoted."""
    assert parse_postprocessing_command('process a(b).srt "{{subtitles}}"', windows=False) == [
        'process', 'a(b).srt', '{{subtitles}}',
    ]


def test_metadata_may_not_follow_interpreter_inline_code():
    """node, perl and ruby keep parsing options after their code text (a second
    --eval= or -e runs more code) and a PHP -r body sits among options, so a
    placeholder after the code could carry provider-supplied metadata in as an
    interpreter option: metadata goes to a script file for them. python's -c
    code leaves plain sys.argv behind, and a shell's remaining arguments are
    positional parameters, so those two keep receiving metadata after the code."""
    with pytest.raises(ValueError, match='after inline code'):
        render('node -e "console.log(\'safe\')" "{{subtitles}}"')
    with pytest.raises(ValueError, match='after inline code'):
        render('perl -e "print \'safe\';" {{release_info}}')
    with pytest.raises(ValueError, match='after inline code'):
        render('ruby -e "puts \'safe\'" {{subtitles}}')
    with pytest.raises(ValueError, match='after inline code'):
        render('php -r "echo \'safe\';" {{episode}}')
    assert render('python -c "print(\'safe\')" "{{subtitles}}"') == [
        'python', '-c', "print('safe')", '/media/a show.en.srt',
    ]
    assert render('sh -c \'printf "%s" "$1"\' process "{{subtitles}}"') == [
        'sh', '-c', 'printf "%s" "$1"', 'process', '/media/a show.en.srt',
    ]


def test_metadata_cannot_select_the_executable():
    with pytest.raises(ValueError):
        render('{{provider}} {{episode}}', provider='/bin/sh')


@pytest.mark.parametrize('program', ['process.cmd', 'process.bat', 'PROCESS.CMD'])
def test_windows_batch_templates_are_rejected(program):
    with mock.patch('utilities.post_processing.os.name', 'nt'):
        with pytest.raises(ValueError):
            render(program + ' "{{release_info}}"', release_info='& calc.exe')


@pytest.mark.parametrize('program', ['copy', 'move', 'del', 'COPY'])
def test_windows_cmd_builtin_commands_are_rejected(program):
    """cmd.exe runs copy, move and del itself; they are not programs an argument
    list can start, so a template naming one can never run and is refused at
    save, whether or not it carries metadata. A real executable such as
    robocopy still runs."""
    with mock.patch('utilities.post_processing.os.name', 'nt'):
        with pytest.raises(ValueError, match='cmd.exe command'):
            render(program + ' /Y "{{release_info}}" D:\\archive', release_info='& calc.exe')


def test_a_windows_executable_that_copies_is_still_accepted():
    with mock.patch('utilities.post_processing.os.name', 'nt'):
        assert render('robocopy "{{release_info}}" D:\\archive', release_info='a & b') == [
            'robocopy', 'a & b', 'D:\\archive']


@pytest.mark.parametrize('command', [
    'source /opt/postprocess.sh {{subtitles}}',
    'export LANG=C {{subtitles}}',
    'ulimit -n 4096 {{subtitles}}',
    'eval {{release_info}}',
])
def test_posix_shell_builtins_are_not_programs(command):
    """The shell built-ins with no executable of their own can never start an
    argument list, so naming one can only be a leftover from shell days. echo,
    kill, printf and test keep real executables and stay allowed."""
    with pytest.raises(ValueError, match='shell built-in'):
        parse_postprocessing_command(command, windows=False)


@pytest.mark.parametrize('command', [
    'awk "{{release_info}}" /tmp/input',
    'sed "{{release_info}}" /tmp/in',
    'sed -n "{{release_info}}" /tmp/in',
    'jq "{{release_info}}" /tmp/in.json',
    "awk 'BEGIN{}' -v {{score}} /tmp/in",
    # GNU option parsing accepts the value attached to its option
    # (-escript, --expression=script, -fscript, -Fsep), and that attached
    # value is program text for the program-carrying options, so metadata
    # embedded there would become executed code.
    'sed -e{{release_info}} /tmp/input',
    'sed --expression={{release_info}} /tmp/input',
    'sed -f{{release_info}} /tmp/in',
    'sed --file={{release_info}} /tmp/in',
    'gawk -e{{release_info}} /tmp/input',
    'awk --source={{release_info}} /tmp/input',
    'awk -vx={{release_info}} /tmp/input',
    'jq -f{{release_info}} /tmp/in.json',
    'jq --from-file={{release_info}} /tmp/in.json',
    # A simple flag may share the same token with the value option behind
    # it, and the value attached after the flag is program text the same
    # way: the flags are consumed and the remainder belongs to the option.
    'sed -ne{{release_info}} /tmp/in',
    'sed -nEe{{release_info}} /tmp/in',
    'sed -rne{{release_info}} /tmp/in',
    'awk -Oe{{release_info}} /tmp/in',
    # An option letter or long name the validator cannot model must not
    # carry metadata either: the placeholder may be the value of an option
    # the real parser understands, and the option itself may be optional
    # attached value (sed -i) or a flag of the tool (sed -b, gawk -b).
    'sed -ae{{release_info}} /tmp/in',
    'sed -be{{release_info}} /tmp/in',
    'sed -ie{{release_info}} /tmp/in',
    'gawk -be{{release_info}} /tmp/in',
    'jq -re{{release_info}} /tmp/in',
])
def test_positional_code_program_text_must_be_fixed(command):
    """awk and its relatives, sed and jq take their program text as the first
    positional argument, so a placeholder there hands provider-supplied
    metadata to the program as executable code (awk's system() runs commands)."""
    with pytest.raises(ValueError, match='must be fixed'):
        parse_postprocessing_command(command, windows=False)


def test_find_exec_must_run_a_fixed_program():
    with pytest.raises(ValueError, match='after -exec must be a fixed program'):
        parse_postprocessing_command('find /media -exec {{provider}} \\;', windows=False)


# The payload after find's -exec is a nested command's own argv, so its
# interior is validated the way the top level is: a placeholder in the
# payload's program position (env runs the word after it), after a
# payload-interior shell's code option, or in an interior carrier option's
# value is refused, at every nesting depth. A + that is not the batch
# terminator (the {} + form) is an ordinary payload argument, so the span
# runs on and the interior behind the stray + is refused too, and the
# {} + batch terminator belongs to -exec and -execdir alone: -ok and
# -okdir prompt for the command once per file and find itself rejects
# the {} + form for them, so the + right behind an -ok payload is an
# ordinary payload argument too and the interior behind it keeps its
# refusal.
@pytest.mark.parametrize('command', [
    'find /media -exec env {{provider}} \\;',
    'find /media -exec sh -c "{{provider}}" \\;',
    'find /media -exec sh -c{{provider}} \\;',
    'find /media -exec /opt/fixer --expression {{provider}} \\;',
    'find /media -exec find /tmp -exec sh -c "{{provider}}" \\; \\;',
    'find /media -exec env -u + sh -c "{{provider}}" \\;',
    'find /media -ok tar -cf out.tar {} + -I {{provider}} \\;',
    'find /media -okdir tar -cf out.tar {} + -I {{provider}} \\;',
])
def test_find_exec_payload_interior_stays_fixed(command):
    with pytest.raises(ValueError):
        parse_postprocessing_command(command, windows=False)


def test_find_exec_payload_keeps_receiving_whole_operands():
    """A fixed payload program with whole-operand placeholder arguments is
    data to the program it names, and so stays open at every depth."""
    assert render('find /media -exec /scripts/fix.sh {{episode}} \\;') == [
        'find', '/media', '-exec', '/scripts/fix.sh', '/media/a show.mkv', ';',
    ]
    assert render('find /media -exec find /tmp -name \'*.srt\' -exec /scripts/fix.sh '
                  '{{subtitles}} \\; \\;') == [
        'find', '/media', '-exec', 'find', '/tmp', '-name', '*.srt', '-exec',
        '/scripts/fix.sh', '/media/a show.en.srt', ';', ';',
    ]


# The whole-argv scan reads a find token the find tables leave unnamed with
# the cross-program rules, so -prune, an action that reads no operand,
# keeps its generic bundle reading there and the e it spells reads the
# argument behind it as the -e carrier's value. When that argument is the
# payload option token itself, the validated span still wins: the span
# walk drew the span from the payload option and the nested validation
# already read the payload with its own program's facts, so the interior
# keeps that reading instead of find's, at the top level and inside the
# nesting the module supports.
@pytest.mark.parametrize('command, expected', [
    ('find /media -prune -exec sort -f "{{subtitles}}" \\;',
     ['find', '/media', '-prune', '-exec', 'sort', '-f', '/media/a show.en.srt', ';']),
    ('find /media -prune -execdir sort -f "{{subtitles}}" \\;',
     ['find', '/media', '-prune', '-execdir', 'sort', '-f', '/media/a show.en.srt', ';']),
    ('find /media -prune -ok sort -f "{{subtitles}}" \\;',
     ['find', '/media', '-prune', '-ok', 'sort', '-f', '/media/a show.en.srt', ';']),
    ('find /media -prune -okdir sort -f "{{subtitles}}" \\;',
     ['find', '/media', '-prune', '-okdir', 'sort', '-f', '/media/a show.en.srt', ';']),
    ('find /media -exec find /tmp -prune -exec sort -f "{{subtitles}}" \\; \\;',
     ['find', '/media', '-exec', 'find', '/tmp', '-prune', '-exec', 'sort', '-f',
      '/media/a show.en.srt', ';', ';']),
])
def test_consumed_payload_option_still_consumes_its_span(command, expected):
    assert render(command) == expected


# The data-taking predicates each read one argument as data, so an operand
# that looks like a payload option is that argument (the -name pattern):
# the span the payload really runs starts at the next payload option, and
# the shell payload behind it is refused, at the top level and inside the
# nesting the module supports.
@pytest.mark.parametrize('command', [
    'find /media -name -exec -exec sh -c "{{provider}}" \\;',
    # -samefile reads one filename operand and the -newerXY tests one
    # reference operand, the same single data argument the other predicates
    # read, so the operand that looks like a payload option never starts
    # the span either.
    'find /media -samefile -exec -exec sh -c "{{provider}}" \\;',
    'find /media -newermt -exec -exec sh -c "{{provider}}" \\;',
    'find /media -exec find /media -name -exec -exec sh -c "{{provider}}" \\; \\;',
    'find /media -exec find /tmp -samefile -exec -exec sh -c "{{provider}}" \\; \\;',
])
def test_find_predicate_data_operand_does_not_start_an_execution_span(command):
    with pytest.raises(ValueError):
        parse_postprocessing_command(command, windows=False)


# find's data actions read their operands as data too, by the counts the
# arity table names (-printf its format, -fprintf the file and the
# format), so an operand that looks like a payload option is that data:
# the span the payload really runs starts at the next payload option, and
# the shell payload behind it is refused, at the top level and inside the
# nesting the module supports.
@pytest.mark.parametrize('command', [
    'find /media -printf -exec -exec sh -c "{{provider}}" \\;',
    'find /media -fprintf /tmp/log -exec -exec sh -c "{{provider}}" \\;',
    'find /media -exec find /tmp -printf -exec -exec sh -c "{{provider}}" \\; \\;',
    'find /media -exec find /tmp -fprintf /tmp/log -exec -exec sh -c "{{provider}}" \\; \\;',
])
def test_find_data_action_operands_do_not_start_an_execution_span(command):
    with pytest.raises(ValueError):
        parse_postprocessing_command(command, windows=False)


# find's output actions -fprint, -fprint0 and -fls read one argument of
# their own too, the file each prints its output to, so their count sits
# in the data-action arity table with -printf's and -fprintf's: an operand
# that looks like a payload option is that file, the span the payload
# really runs starts at the next payload option, and the shell payload
# behind it is refused, at the top level and inside the nesting the
# module supports.
@pytest.mark.parametrize('command', [
    'find /media -fprint -exec -exec sh -c "{{provider}}" \\;',
    'find /media -fprint0 -exec -exec sh -c "{{provider}}" \\;',
    'find /media -fls -exec -exec sh -c "{{provider}}" \\;',
    'find /media -exec find /tmp -fprint -exec -exec sh -c "{{provider}}" \\; \\;',
    'find /media -exec find /tmp -fprint0 -exec -exec sh -c "{{provider}}" \\; \\;',
    'find /media -exec find /tmp -fls -exec -exec sh -c "{{provider}}" \\; \\;',
])
def test_find_output_action_operands_do_not_start_an_execution_span(command):
    with pytest.raises(ValueError):
        parse_postprocessing_command(command, windows=False)


# find's operand-reading options outside the tests and actions read one
# plain data argument each: -files0-from reads the file it takes its
# starting points from, -regextype the regex syntax the -regex and
# -iregex tests parse their patterns with, and -D the debug flags find
# prints its diagnostics under, warning and continuing past a flag it
# does not recognise, so an operand that looks like a payload option is
# that data (the first -exec below is -files0-from's file operand and
# -D's debug-flag operand), and the span the payload really runs starts
# at the payload option behind it, at the top level and inside the
# nesting the module supports.
@pytest.mark.parametrize('command', [
    'find -files0-from -exec -exec sh -c "{{provider}}" \\;',
    'find -regextype -exec -exec sh -c "{{provider}}" \\;',
    'find -D -exec /media -exec sh -c "{{provider}}" \\;',
    'find /media -exec find -files0-from -exec -exec sh -c "{{provider}}" \\; \\;',
    'find /media -exec find -regextype -exec -exec sh -c "{{provider}}" \\; \\;',
])
def test_find_global_data_option_operands_do_not_start_an_execution_span(command):
    with pytest.raises(ValueError):
        parse_postprocessing_command(command, windows=False)


# find's data predicates and actions read their operands as plain data,
# so a placeholder pattern or format stays open: the carrier scan consumes
# the operand with its token by the same arity the span walk uses instead
# of reading the predicate as a short bundle, at the top level and inside
# the nesting the module supports.
@pytest.mark.parametrize('command, expected', [
    ('find /media -name "{{episode_name}}" -print',
     ['find', '/media', '-name', 'a show', '-print']),
    ('find /media -printf "{{episode_name}}" -print',
     ['find', '/media', '-printf', 'a show', '-print']),
    ('find /media -exec find /tmp -name "{{episode_name}}" -print \\;',
     ['find', '/media', '-exec', 'find', '/tmp', '-name', 'a show', '-print', ';']),
    ('find /media -exec find /tmp -printf "{{episode_name}}" -print \\;',
     ['find', '/media', '-exec', 'find', '/tmp', '-printf', 'a show', '-print', ';']),
])
def test_find_data_operands_stay_open(command, expected):
    assert render(command) == expected


@pytest.mark.parametrize('command, expected', [
    ("awk '/plain/ {print}' {{subtitles}}",
     ['awk', '/plain/ {print}', '/media/a show.en.srt']),
    ("sed 's/a/b/' {{subtitles}}", ['sed', 's/a/b/', '/media/a show.en.srt']),
    ('jq .fixed {{subtitles}}', ['jq', '.fixed', '/media/a show.en.srt']),
    ('find /media -name \'*.srt\' -exec /opt/fix.sh {{subtitles}} \\;',
     ['find', '/media', '-name', '*.srt', '-exec', '/opt/fix.sh', '/media/a show.en.srt', ';']),
    # The same program forms attached to their option: the attached value is
    # fixed program text, so the placeholders behind it are input files.
    ("sed -e's/a/b/' {{subtitles}}", ['sed', '-es/a/b/', '/media/a show.en.srt']),
    ('sed --expression=s/a/b/ {{subtitles}}',
     ['sed', '--expression=s/a/b/', '/media/a show.en.srt']),
    ('sed -f/opt/scripts/fix.sed {{subtitles}}',
     ['sed', '-f/opt/scripts/fix.sed', '/media/a show.en.srt']),
    # Flags in front of the program option keep the same GNU reading: the
    # attached value behind them is the fixed program, the placeholders are
    # input files.
    ("sed -ne's/a/b/' {{subtitles}}", ['sed', "-nes/a/b/", '/media/a show.en.srt']),
    ("gawk -be'BEGIN{print 1}' {{subtitles}}",
     ['gawk', '-beBEGIN{print 1}', '/media/a show.en.srt']),
    # An optional attached value (sed -i[SUFFIX]) is the option's value too,
    # and the fixed script keeps its place behind it.
    ("sed -i.bak 's/a/b/' {{subtitles}}",
     ['sed', '-i.bak', 's/a/b/', '/media/a show.en.srt']),
])
def test_positional_code_programs_can_receive_file_data(command, expected):
    """After the fixed program text a placeholder is file or positional data,
    the way it is for a plain script."""
    assert render(command) == expected


# An option whose value the program can execute refuses placeholders for
# every program alike, not only the ones the per-family rules model. The
# attached value slips past those rules once the fixed program text is in
# place, so the cross-program grammar is what catches it.
@pytest.mark.parametrize('command', [
    'sed "s/a/b/" --expression={{release_info}} /tmp/in',
    'sed "s/a/b/" -e{{release_info}} /tmp/in',
    'gawk "BEGIN{}" -f{{release_info}} /tmp/in',
    "awk 'BEGIN{}' --source={{release_info}} /tmp/in",
    # The separate operand keeps the refusal the per-family rules give it.
    'sed "s/a/b/" --expression {{release_info}} /tmp/in',
    'sed "s/a/b/" -e {{release_info}} /tmp/in',
])
def test_script_option_values_stay_fixed(command):
    with pytest.raises(ValueError, match='must be fixed'):
        parse_postprocessing_command(command, windows=False)


@pytest.mark.parametrize('command', [
    'tar --checkpoint-action=exec={{release_info}} -cf out.tar /media',
    'tar --checkpoint-action exec={{release_info}} -cf out.tar /media',
])
def test_command_option_values_stay_fixed(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        parse_postprocessing_command(command, windows=False)


# These options hand their value to a program that runs it: tar's
# --to-command pipes each extracted file to the command, tar's -I is the
# short form of --use-compress-program, tar's --info-script and
# --new-volume-script name the scripts tar runs at volume changes, and
# sort's --compress-program names the program sort runs to compress its
# temporary output. tar's remote pair joins them: --rsh-command names the
# shell tar reaches a remote archive through and --rmt-command the tape
# program it runs on the far side instead of its own rmt. The attached,
# separate and abbreviated spellings are refused alike, in a find payload
# and behind a wrapper too, and the abbreviation is the proper prefix a
# GNU parser could resolve the token to. The I value rides in through
# the old-style block as well: GNU tar reads a first undashed argument as
# a bundle of option letters whose value-taking letters read the
# arguments behind the bundle in order (cfI out.tar prog is -c -f
# out.tar -I prog), so the block's I operand is refused the way the
# dashed -I's value is.
@pytest.mark.parametrize('command', [
    'tar --to-command={{release_info}} -xf /tmp/archive.tar',
    'tar --to-command {{release_info}} -xf /tmp/archive.tar',
    'tar --to-comm={{release_info}} -xf /tmp/archive.tar',
    'find /media -exec tar --to-command={{release_info}} -xf /tmp/archive.tar \\;',
    'tar -I {{release_info}} -cf out.tar /media',
    'tar -I{{release_info}} -cf out.tar /media',
    'find /media -exec tar -I {{release_info}} -cf out.tar /media \\;',
    'nice tar -I {{release_info}} -cf out.tar /media',
    'tar cfI out.tar {{provider}} /media',
    'sort --compress-program={{release_info}} -o /tmp/out /tmp/in',
    'sort --compress-program {{release_info}} -o /tmp/out /tmp/in',
    'sort --compress-prog={{release_info}} -o /tmp/out /tmp/in',
    'find /media -exec sort --compress-program={{release_info}} -o /tmp/out /tmp/in \\;',
    'nice sort --compress-program={{release_info}} -o /tmp/out /tmp/in',
    'tar --info-script={{release_info}} -cf out.tar /media',
    'tar --info-script {{release_info}} -cf out.tar /media',
    'tar --new-volume-script={{release_info}} -cf out.tar /media',
    'tar --new-volume-script {{release_info}} -cf out.tar /media',
    'tar --info-scr={{release_info}} -cf out.tar /media',
    'find /media -exec tar --info-script={{release_info}} -cf out.tar /media \\;',
    'tar --rsh-command={{provider}} -cf host:/tmp/archive.tar /media',
    'tar --rmt-command={{provider}} -cf host:/tmp/archive.tar /media',
])
def test_program_running_option_values_stay_fixed(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# rsync runs the value of --rsync-path on the remote side and the value of
# --rsh as the transport shell, both through a shell, so a placeholder there
# is remote code; the attached and the separate-value forms are refused
# alike, and -e, rsync's short spelling of --rsh, is covered by the generic
# carrier letter.
@pytest.mark.parametrize('command', [
    'rsync --rsync-path={{release_info}} -a src dest',
    'rsync --rsync-path {{release_info}} -a src dest',
    'rsync --rsh={{release_info}} -a src dest',
    'rsync --rsh {{release_info}} -a src dest',
    'rsync -e {{release_info}} -a src dest',
])
def test_remote_execution_carrier_values_stay_fixed(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        parse_postprocessing_command(command, windows=False)


# rsync's -E (executability) and -F (its --filter shortcut) are value-less
# flags, so neither is the value-taking -e/-f carrier its lowercase spells:
# each leaves the element behind it its own reading, and the carrier there
# keeps its refusal, with its separate value and behind a bare -- that a
# data option keeps as its own value, at the top level, in a find payload
# and behind a wrapper. sort's -f is the ignore-case flag the same way,
# value-less even though its letter is the file carrier's own lowercase:
# the documented letter resolves ahead of the cross-program reading, alone
# and in a cluster of documented letters (-bf), and the compression
# program behind it keeps its refusal.
@pytest.mark.parametrize('command', [
    'rsync -E --rsh {{release_info}} -a src host:dest',
    'find /media -exec rsync -E --rsh {{release_info}} -a src host:dest \\;',
    'nice rsync -E --rsh {{release_info}} -a src host:dest',
    'rsync -E --suffix -- --rsh={{release_info}} -a src host:dest',
    'find /media -exec rsync -E --suffix -- --rsh={{release_info}} -a src host:dest \\;',
    'rsync -F --suffix -- --rsync-path={{release_info}} -a src host:dest',
    'find /media -exec rsync -F --suffix -- --rsync-path={{release_info}} -a src host:dest \\;',
    'sort -f --compress-program {{provider}} /tmp/in',
    'sort -bf --compress-program {{provider}} /tmp/in',
    'find /media -exec sort -f --compress-program {{provider}} /tmp/in \\;',
    'nice sort -f --compress-program {{provider}} /tmp/in',
])
def test_documented_flag_letters_do_not_hide_carriers(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# tar's documented value-less flags are never the carriers their names
# prefix: --checkpoint enables the checkpoint display and --compress
# names the compress filter, and neither ever takes a value from the next
# argument (the checkpoint's optional count attaches with the separator),
# so neither is --checkpoint-action or --compress-program, and the carrier
# option behind each keeps its own reading and its refusal. sort's --check
# joins them: the check flag is not --checkpoint-action the name prefixes,
# and its optional argument attaches with the separator and never comes
# from the next argument, so the compression program behind it stays live.
@pytest.mark.parametrize('command', [
    'tar --checkpoint --checkpoint-action exec={{provider}} -cf out.tar /media',
    'tar --compress --checkpoint-action exec={{provider}} -cf out.tar /media',
    'sort --check --compress-program {{provider}} /tmp/in',
])
def test_documented_value_less_long_flags_do_not_hide_carriers(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# A documented value-less flag reads nothing behind it, so the element
# after it keeps its own reading: sort's -f is its ignore-case flag and
# its --check the check flag, so the operand behind either is an input
# filename the placeholder fills as plain data, and --check's optional
# argument attaches with the separator and never comes from the next
# argument, so its attached value is data the way every attached value of
# a value-less flag is.
def test_documented_value_less_flags_leave_the_next_element_open():
    assert render('sort -f {{subtitles}} /tmp/in') == [
        'sort', '-f', '/media/a show.en.srt', '/tmp/in',
    ]
    assert render('sort --check {{subtitles}} /tmp/in') == [
        'sort', '--check', '/media/a show.en.srt', '/tmp/in',
    ]
    assert render('sort --check={{provider}} /tmp/in') == [
        'sort', '--check=provider', '/tmp/in',
    ]


# rsync's -T reads its directory operand and its -M the option it
# forwards to the remote side, plain data to the local scan both, the way
# tar's -C does, so the operand is consumed before the carrier scan reads
# what follows: the first --rsh is the data value, and the live transport
# option behind it keeps its refusal, at the top level and inside a find
# payload. sort's -o reads its output filename and its -T the directory
# it keeps its temporary files in, data however the name is spelled, so
# the first --compress-program is that filename or directory and the
# compression program sort runs behind it keeps its refusal too.
@pytest.mark.parametrize('command', [
    'rsync -T --rsh --rsh {{provider}} src host:dest',
    'find /media -exec rsync -T --rsh --rsh {{provider}} src host:dest \\;',
    'rsync -M --rsh --rsh {{provider}} src host:dest',
    'sort -o --compress-program --compress-program {{provider}} /media/input',
    'sort -T --compress-program --compress-program {{provider}} /media/input',
])
def test_documented_data_option_letters_do_not_hide_carriers(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# rsync's -T carries its directory operand glued to the letter or in the
# next argument, and the operand is plain data either way, so a
# placeholder there rides in as data and the carrier scan never reads the
# attached value as a bundle of names. rsync's -M and sort's -o read
# their operands in the next argument the same way, the option rsync
# forwards to the remote side and the filename sort writes its output to,
# so a placeholder there rides in as data too.
def test_documented_data_option_letter_operands_stay_open():
    assert render('rsync -T=/tmp src {{subtitles}} host:dest') == [
        'rsync', '-T=/tmp', 'src', '/media/a show.en.srt', 'host:dest',
    ]
    assert render('rsync -T{{directory}} src host:dest') == [
        'rsync', '-T/media', 'src', 'host:dest',
    ]
    assert render('rsync -M {{provider}} -a src host:dest') == [
        'rsync', '-M', 'provider', '-a', 'src', 'host:dest',
    ]
    assert render('sort -o {{subtitles}} /tmp/in') == [
        'sort', '-o', '/media/a show.en.srt', '/tmp/in',
    ]
    assert render('sort -T {{subtitles}} /tmp/in') == [
        'sort', '-T', '/media/a show.en.srt', '/tmp/in',
    ]


# A documented data-option operand is consumed as plain data before the
# carrier scan reads what follows, so the live option behind it keeps its
# refusal: the first --rsh is the --exclude pattern or the --suffix backup
# suffix operand rsync reads as data, rsync's --remote-option the option
# it forwards to the remote side to parse there, data to the local scan
# however it is spelled, and the first --checkpoint-action the
# --files-from filename or the --directory operand tar reads as data,
# while the transport and execution options behind them stay live, at the
# top level and inside a find payload. sort's path operands join them:
# --output reads the filename it writes its output to,
# --temporary-directory the directory of its temporary files,
# --random-source the file it seeds its random hash from and
# --files0-from the file of the input names, each a path however it is
# spelled, so the first --compress-program is that path and the
# compression program behind it stays live.
@pytest.mark.parametrize('command', [
    'rsync --exclude --rsh --rsh {{provider}} src host:dest',
    'find /media -exec rsync --exclude --rsh --rsh {{provider}} src host:dest \\;',
    'tar --checkpoint=1 -cf out.tar --files-from --checkpoint-action '
    '--checkpoint-action exec={{provider}} /media',
    'find /media -exec tar --checkpoint=1 -cf out.tar --files-from '
    '--checkpoint-action --checkpoint-action exec={{provider}} /media \\;',
    'tar --checkpoint=1 -cf out.tar -T --checkpoint-action '
    '--checkpoint-action exec={{provider}} /media',
    'tar --directory --checkpoint-action --checkpoint-action exec={{provider}} -cf out.tar /media',
    'find /media -exec tar --directory --checkpoint-action '
    '--checkpoint-action exec={{provider}} -cf out.tar /media \\;',
    'rsync --suffix --rsh --rsh {{provider}} src host:dest',
    'find /media -exec rsync --suffix --rsh --rsh {{provider}} src host:dest \\;',
    'rsync --remote-option --rsh --rsh {{provider}} src host:dest',
    'sort --output --compress-program --compress-program {{provider}} /media/input',
    'sort --temporary-directory --compress-program --compress-program {{provider}} /media/input',
    'sort --random-source --compress-program --compress-program {{provider}} /media/input',
    'sort --files0-from --compress-program --compress-program {{provider}} /media/input',
])
def test_documented_data_option_operands_do_not_hide_carriers(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# GNU option parsing accepts unambiguous prefixes of long option names,
# so an abbreviated data-option spelling consumes the element behind it
# as that option's operand exactly as the full spelling does: --files-f
# is tar's --files-from, the first --checkpoint-action its file-list
# operand, so the execution option behind it stays live. A genuinely
# ambiguous prefix leaves the operand unconsumed, because more than one
# documented data option of the installed GNU tar starts with --own
# (--owner, --owner-map), so the carrier behind it keeps its reading.
@pytest.mark.parametrize('command', [
    'tar --files-f --checkpoint-action --checkpoint-action exec={{provider}} -cf out.tar /media',
    'tar --own --checkpoint-action exec={{provider}} -cf out.tar /media',
])
def test_abbreviated_data_option_operands_do_not_hide_carriers(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The documented data-option operands themselves stay open to metadata
# the way every plain data operand is: rsync's --exclude pattern, attached
# or separate, its --files-from file list, its --remote-option operand and
# its -f filter rule are data rsync reads, and tar's -T file list is data
# tar reads, glued or separate, and sort reads its --output filename and
# its --temporary-directory, --random-source and --files0-from paths as
# data the same way.
def test_documented_data_option_operand_values_stay_open():
    assert render('rsync --exclude={{episode_name}} -a src host:dest') == [
        'rsync', '--exclude=a show', '-a', 'src', 'host:dest',
    ]
    assert render('rsync --exclude {{provider}} -a src host:dest') == [
        'rsync', '--exclude', 'provider', '-a', 'src', 'host:dest',
    ]
    assert render('rsync --files-from {{subtitles}} -a src host:dest') == [
        'rsync', '--files-from', '/media/a show.en.srt', '-a', 'src', 'host:dest',
    ]
    assert render('rsync --remote-option {{provider}} -a src host:dest') == [
        'rsync', '--remote-option', 'provider', '-a', 'src', 'host:dest',
    ]
    assert render('rsync -f {{provider}} -a src host:dest') == [
        'rsync', '-f', 'provider', '-a', 'src', 'host:dest',
    ]
    assert render('tar -cf out.tar -T{{subtitles}} /media') == [
        'tar', '-cf', 'out.tar', '-T/media/a show.en.srt', '/media',
    ]
    assert render('sort --output {{subtitles}} /tmp/in') == [
        'sort', '--output', '/media/a show.en.srt', '/tmp/in',
    ]
    assert render('sort --temporary-directory {{subtitles}} /tmp/in') == [
        'sort', '--temporary-directory', '/media/a show.en.srt', '/tmp/in',
    ]
    assert render('sort --random-source {{provider}} /tmp/in') == [
        'sort', '--random-source', 'provider', '/tmp/in',
    ]
    assert render('sort --files0-from {{subtitles}} /tmp/in') == [
        'sort', '--files0-from', '/media/a show.en.srt', '/tmp/in',
    ]


# A documented data option resolves ahead of the carrier-prefix fallback,
# the way GNU option parsing resolves a name before a prefix: rsync's
# --info is its verbosity flag list, not --info-script, and tar's --so is
# its --sort abbreviated, not --source, so the value each reads stays the
# data the program documents.
@pytest.mark.parametrize('command, expected', [
    ('rsync --info={{release_info}} src host:dest',
     ['rsync', '--info=release', 'src', 'host:dest']),
    ('tar --so={{release_info}} -cf out.tar /media',
     ['tar', '--so=release', '-cf', 'out.tar', '/media']),
])
def test_documented_data_option_names_beat_carrier_prefixes(command, expected):
    assert render(command) == expected


# The operand an execution-capable option keeps reading as its payload is
# the executed value too, for a tool outside the tables as much as for the
# awk script file or the find -exec command the shape comes from.
@pytest.mark.parametrize('command', [
    '/opt/fixer -f {{release_info}} /media/input',
    '/opt/fixer -exec {{release_info}} \\;',
])
def test_executing_option_payloads_stay_fixed(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        parse_postprocessing_command(command, windows=False)


# A short cluster behind an equals separator keeps the carrier reading: the
# parser hands the text from the separator on to the carrier as its value
# (-ne={{...}}, where sed's -e receives the script ={{...}}), so the
# placeholder rides the value with the separator as without it, and the
# plain attached forms stay covered beside the equals forms.
@pytest.mark.parametrize('command', [
    '/opt/fixer -ne={{release_info}} /media/input',
    '/opt/fixer -nf={{episode}} /media/input',
    '/opt/fixer -ne{{release_info}} /media/input',
    '/opt/fixer -nf{{episode}} /media/input',
])
def test_short_cluster_carrier_values_stay_fixed_with_or_without_equals(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        parse_postprocessing_command(command, windows=False)


# An option named like an execution carrier is refused even when a custom
# tool means it as plain data; the temporary strictness is the price of one
# grammar holding for the programs the per-family rules do not model. GNU
# option parsing also accepts unambiguous prefixes of long option names, so
# a proper prefix of a carrier name is refused as the carrier a GNU parser
# could resolve it to, with its value attached and as a separate operand.
@pytest.mark.parametrize('command', [
    '"/opt/my tools/process" --file={{episode}} "prefix {{subtitles}} suffix"',
    'python /opt/scripts/capture.py --file={{episode}} "{{release_info}}"',
    '/opt/fixer --checkpoint-act=exec={{release_info}} /media/input',
    '/opt/fixer --checkpoint-act exec={{release_info}} /media/input',
])
def test_carrier_named_options_refuse_placeholders_for_custom_tools_too(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The name of an option must be fixed the way its program position is: the
# rendering can turn a placeholder in the name into a carrier's attached
# form, so the position it fills cannot be vouched for as data. In a short
# bundle the cluster is read positionally, so a placeholder at or in front
# of the carrier letter is part of the names, behind it the value's.
@pytest.mark.parametrize('command', [
    '/opt/fixer --{{release_info}} /media/input',
    '/opt/fixer --check{{release_info}} /media/input',
    '/opt/fixer -{{release_info}} /media/input',
    '/opt/fixer -{{provider}} {{release_info}}',
    '/opt/fixer -{{provider}}e /media/input',
])
def test_option_names_containing_placeholders_stay_fixed(command):
    with pytest.raises(ValueError, match='must be fixed'):
        parse_postprocessing_command(command, windows=False)


def test_data_operands_after_a_separator_stay_open():
    """An unconsumed -- ends option parsing for its command's argv, at the
    top level and inside a find payload, so the tokens after it are data
    operands; the same shapes without the terminator keep their refusal."""
    assert render('/opt/fixer -- --expression={{release_info}}') == [
        '/opt/fixer', '--', '--expression=release',
    ]
    assert render('find /media -exec /opt/fixer -- --expression={{release_info}} \\;') == [
        'find', '/media', '-exec', '/opt/fixer', '--', '--expression=release', ';',
    ]
    with pytest.raises(ValueError, match='can carry executable code'):
        render('/opt/fixer --expression={{release_info}}')
    with pytest.raises(ValueError, match='can carry executable code'):
        render('find /media -exec /opt/fixer --expression={{release_info}} \\;')


# A -- a carrier read as its separate value was consumed, so it does not end
# option parsing: the options after it stay options, and a placeholder reaches
# the carrier behind them, at the top level and inside a find payload.
@pytest.mark.parametrize('command', [
    'rsync --rsync-path -- --rsh={{release_info}} -a src host:dest',
    'find /media -exec rsync --rsync-path -- --rsh={{release_info}} -a src host:dest \\;',
])
def test_separator_consumed_as_carrier_value_keeps_option_parsing(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# By the GNU convention a bare -- right behind an option that has read no
# value yet is that option's separate value, so it does not end option
# parsing and a placeholder still reaches the carrier behind it, at the top
# level and inside a find payload.
@pytest.mark.parametrize('command', [
    'rsync --suffix -- --rsh={{release_info}} -a src host:dest',
    'find /media -exec rsync --suffix -- --rsh={{release_info}} -a src host:dest \\;',
    'tar --label -- --checkpoint=1 --checkpoint-action=exec={{release_info}} -cf out.tar /media',
    'find /media -exec tar --label -- --checkpoint=1 '
    '--checkpoint-action=exec={{release_info}} -cf out.tar /media \\;',
])
def test_separator_consumed_as_data_option_value_keeps_option_parsing(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The token a carrier read as its separate value is never scanned as an
# option itself, so the -- behind it ends option parsing and the data
# operands after it stay open. The boundaries keep their readings: a --
# after the program or after a data operand ends parsing, and an option
# with an attached value does not take a following -- as its own.
def test_separator_behind_carrier_value_ends_option_parsing():
    assert render('/opt/fixer --exec --file -- --expression={{release_info}}') == [
        '/opt/fixer', '--exec', '--file', '--', '--expression=release',
    ]
    assert render('/opt/fixer --label=x -- --expression={{release_info}}') == [
        '/opt/fixer', '--label=x', '--', '--expression=release',
    ]
    assert render('/opt/fixer operand -- --expression={{release_info}}') == [
        '/opt/fixer', 'operand', '--', '--expression=release',
    ]
    assert render('/opt/fixer -- --expression={{release_info}}') == [
        '/opt/fixer', '--', '--expression=release',
    ]


# A data option the program documents reads its operand as plain data, so
# the carrier-looking token behind it stays open: tar's --label reads the
# next element as the literal archive label, and a placeholder there is
# metadata in the label, at the top level and inside a find payload.
def test_documented_data_option_operands_stay_open():
    assert render('tar --label --expression={{release_info}} -cf /tmp/archive.tar /tmp/input') == [
        'tar', '--label', '--expression=release', '-cf', '/tmp/archive.tar', '/tmp/input',
    ]
    assert render('find /media -exec tar --label --expression={{release_info}} '
                  '-cf /tmp/archive.tar /tmp/input \\;') == [
        'find', '/media', '-exec', 'tar', '--label', '--expression=release',
        '-cf', '/tmp/archive.tar', '/tmp/input', ';',
    ]


# tar's -C carries its directory operand glued to the letter, so the token
# has already read its value: the bare -- behind it stays the separator,
# exactly as behind an attached-value long option, and the checkpoint
# action behind it a data operand, at the top level and inside a find
# payload.
def test_glued_data_option_value_keeps_the_separator():
    assert render('tar -cf out.tar -C/media -- --checkpoint-action=exec={{release_info}}') == [
        'tar', '-cf', 'out.tar', '-C/media', '--', '--checkpoint-action=exec=release',
    ]
    assert render('find /media -exec tar -cf out.tar -C/media '
                  '-- --checkpoint-action=exec={{release_info}} \\;') == [
        'find', '/media', '-exec', 'tar', '-cf', 'out.tar', '-C/media', '--',
        '--checkpoint-action=exec=release', ';',
    ]


# A short bundle whose carrier letter carries its value glued to it
# (-essh, the -e carrier with ssh attached) has already read its value
# exactly as an equals-attached long option has, so the bare -- behind the
# token is the separator and the carrier-looking token behind it a data
# operand, at the top level and inside a find payload.
@pytest.mark.parametrize('command, expected', [
    ('rsync -essh -- --rsh={{release_info}} src host:dest',
     ['rsync', '-essh', '--', '--rsh=release', 'src', 'host:dest']),
    ('find /media -exec rsync -essh -- --rsh={{release_info}} src host:dest \\;',
     ['find', '/media', '-exec', 'rsync', '-essh', '--', '--rsh=release', 'src', 'host:dest', ';']),
])
def test_glued_carrier_value_keeps_the_separator(command, expected):
    assert render(command) == expected


# A documented value-less flag reads nothing behind it, so the bare --
# right behind one is the separator, not a value the flag would take:
# rsync's -E, tar's -v and sort's -f leave the -- the separator's
# reading and the carrier-looking token behind it a data operand, at the
# top level and inside a find payload.
def test_separator_behind_a_documented_value_less_flag_stays_the_separator():
    assert render('rsync -E -- --rsh={{release_info}} src host:dest') == [
        'rsync', '-E', '--', '--rsh=release', 'src', 'host:dest',
    ]
    assert render('find /media -exec rsync -E -- --rsh={{release_info}} src host:dest \\;') == [
        'find', '/media', '-exec', 'rsync', '-E', '--', '--rsh=release', 'src', 'host:dest', ';',
    ]
    assert render('tar -v -- --checkpoint-action=exec={{provider}}') == [
        'tar', '-v', '--', '--checkpoint-action=exec=provider',
    ]
    assert render('sort -f -- --compress-program={{provider}} /tmp/in') == [
        'sort', '-f', '--', '--compress-program=provider', '/tmp/in',
    ]


# rsync's documented value-less short flags, its -a archive mode among
# them, and a cluster of them (-av) read nothing behind them, so the bare
# -- behind one is the separator and the carrier-looking token behind it
# a data operand, at the top level and inside a find payload. sort's -bf
# cluster keeps the same reading.
@pytest.mark.parametrize('command, expected', [
    ('rsync -a -- --rsh={{release_info}} src host:dest',
     ['rsync', '-a', '--', '--rsh=release', 'src', 'host:dest']),
    ('rsync -av -- --rsh={{release_info}} src host:dest',
     ['rsync', '-av', '--', '--rsh=release', 'src', 'host:dest']),
    ('find /media -exec rsync -a -- --rsh={{release_info}} src host:dest \\;',
     ['find', '/media', '-exec', 'rsync', '-a', '--', '--rsh=release', 'src', 'host:dest', ';']),
    ('find /media -exec rsync -av -- --rsh={{release_info}} src host:dest \\;',
     ['find', '/media', '-exec', 'rsync', '-av', '--', '--rsh=release', 'src', 'host:dest', ';']),
    ('sort -bf -- --compress-program={{provider}} /tmp/in',
     ['sort', '-bf', '--', '--compress-program=provider', '/tmp/in']),
])
def test_documented_flag_clusters_keep_the_separator(command, expected):
    assert render(command) == expected


# rsync's documented value-less long flags, --archive the long spelling of
# the -a archive mode and --copy-links the -L symlink transform among
# them, tar's --verbose and sort's --stable read nothing behind them, so
# the bare -- right behind one is the separator and the carrier-looking
# token behind it a data operand, at the top level and inside a find
# payload.
@pytest.mark.parametrize('command, expected', [
    ('rsync --archive -- --rsh={{release_info}} src host:dest',
     ['rsync', '--archive', '--', '--rsh=release', 'src', 'host:dest']),
    ('find /media -exec rsync --archive -- --rsh={{release_info}} src host:dest \\;',
     ['find', '/media', '-exec', 'rsync', '--archive', '--', '--rsh=release',
      'src', 'host:dest', ';']),
    ('rsync --copy-links -- --rsh={{release_info}} src host:dest',
     ['rsync', '--copy-links', '--', '--rsh=release', 'src', 'host:dest']),
    ('tar --verbose -- --checkpoint-action=exec={{provider}}',
     ['tar', '--verbose', '--', '--checkpoint-action=exec=provider']),
    ('sort --stable -- --compress-program={{provider}} /tmp/in',
     ['sort', '--stable', '--', '--compress-program=provider', '/tmp/in']),
    # rsync negates its options with --no- and GNU option parsing accepts
    # unambiguous abbreviations, so the value-less reading holds behind a
    # supported negation and behind an abbreviation only one entry of the
    # value-less set matches, at the top level and inside a find payload.
    ('rsync --no-archive -- --rsh={{release_info}} src host:dest',
     ['rsync', '--no-archive', '--', '--rsh=release', 'src', 'host:dest']),
    ('rsync --arch -- --rsh={{release_info}} src host:dest',
     ['rsync', '--arch', '--', '--rsh=release', 'src', 'host:dest']),
    ('find /media -exec rsync --no-archive -- --rsh={{release_info}} src host:dest \\;',
     ['find', '/media', '-exec', 'rsync', '--no-archive', '--', '--rsh=release',
      'src', 'host:dest', ';']),
])
def test_documented_value_less_long_flags_keep_the_separator(command, expected):
    assert render(command) == expected


# A long option the arity tables leave undocumented keeps the
# GNU-convention value reading the pinned rows rely on: the bare -- right
# behind it is that option's separate value, so it does not end option
# parsing and the placeholder still reaches the carrier behind it. A
# genuinely ambiguous abbreviation keeps the reading too: more than one
# documented value-less flag of the installed rsync starts with --a.
def test_undocumented_value_less_long_options_keep_the_gnu_convention_reading():
    with pytest.raises(ValueError, match='can carry executable code'):
        render('rsync --nonsense -- --rsh={{release_info}} src host:dest')
    with pytest.raises(ValueError, match='can carry executable code'):
        render('rsync --a -- --rsh={{release_info}} src host:dest')


# A wrapper consumes its own -- as the end of its options, so the program it
# wraps begins after it without a separator of its own: the child's executable
# options keep their refusal, at the top level and inside a find payload.
@pytest.mark.parametrize('command', [
    'nice -- tar --checkpoint=1 --checkpoint-action=exec={{release_info}} -cf out.tar /media',
    'find /media -exec nice -- tar --checkpoint=1 --checkpoint-action=exec={{release_info}} '
    '-cf out.tar \\;',
])
def test_wrapper_separator_does_not_hide_child_options(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# A wrapper's value-taking option carries its value either as the argument
# behind it (-c 0) or attached to its spelling (-c0, --cpu-list=0), the
# GNU attached form, and the walks resolve the attached value to the
# option's own rules: the value rides with the token, so the walk consumes
# the token alone, the option that removes the positional operand
# (taskset's -c, runuser's -u) removes it on its attached spelling too,
# and the child program keeps its true position. Without the resolution
# the walks left the spelling unrecognized, consumed the child program as
# the positional operand and drew the boundary one word late, out.tar in
# the program position, where tar's -I carrier never resolves and the
# program it runs behind the wrapper kept its placeholder.
@pytest.mark.parametrize('command', [
    'taskset -c0 tar -cf out.tar -I {{provider}} /media',
    'taskset --cpu-list=0 tar -cf out.tar -I {{provider}} /media',
    'runuser -unobody tar -cf out.tar -I {{provider}} /media',
    'runuser --user=nobody tar -cf out.tar -I {{provider}} /media',
])
def test_attached_wrapper_option_values_do_not_hide_the_child(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# GNU option parsing bundles short options into one token: every letter
# that takes no value stands on its own in the cluster, and the first
# letter that takes a value ends the cluster, reading the rest of the
# token as its attached value (-punobody is -p and -u with nobody behind
# it) or, when the cluster ends at that letter, awaiting the argument
# behind it (-nw 5, -cn 5, -rn 5, -Ep prompt). The walks resolve a cluster
# letter by letter with each letter's arity, so the value a cluster
# carries never takes the child program's place: the attached value rides
# with the token, the awaited one is consumed behind it, and the resolved
# value-taking name keeps its rules, the value it reads and the
# positional operand it removes (runuser's -u). The misreading swallowed
# the child as the wrapper's positional operand (runuser's user) or left
# the unawaited value in the child position (flock's timeout, watch's
# interval, xargs's limit, sudo's prompt), and the child's -I carrier
# went unchecked behind every one of them. The spellings the installed
# tools reject are not rowed: taskset reads its cpu list only as a
# separate argument, and the real runuser refuses -l together with -u as
# mutually exclusive, so neither hides a child a real tool runs.
@pytest.mark.parametrize('command', [
    'runuser -punobody tar -cf out.tar -I {{provider}} /media',
    'runuser -munobody tar -cf out.tar -I {{provider}} /media',
    'sudo -Ep prompt tar -cf out.tar -I {{provider}} /media',
    'flock -nw 5 /tmp/lock tar -cf out.tar -I {{provider}} /media',
    'watch -cn 5 tar -cf out.tar -I {{provider}} /media',
    'xargs -rn 5 tar -cf out.tar -I {{provider}} /media',
])
def test_clustered_wrapper_options_do_not_hide_the_child(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The wrapper walks read a short option with the case the GNU parser reads
# it in: an uppercase letter spells the option of its own and never the
# lowercase one beside it, so xargs's -P max-procs and sudo's -C close-from
# and -D chdir carry their values behind their letters, where a lowercased
# comparison left the letters unmatched and the walk consumed the value as
# the child program. The tables carry the arity the real options have:
# ionice's -t is the ignore flag and reads nothing, and xargs's -i, -l and
# -e take their replace string, line count and eof string only attached to
# their own spelling (-i{}, -l2), never a separate argument, so a bare
# spelling reads nothing behind it and the program sits in the program
# position (the installed findutils names the separate form unreal: -l 2
# makes 2 the command it runs). Behind every one of the misreadings the
# child's -I carrier went unchecked.
@pytest.mark.parametrize('command', [
    'xargs -P 2 tar -cf out.tar -I {{provider}} /media',
    'sudo -C 5 tar -cf out.tar -I {{provider}} /media',
    'sudo -D /tmp tar -cf out.tar -I {{provider}} /media',
    'ionice -t tar -cf out.tar -I {{provider}} /media',
    'xargs -i tar -cf out.tar -I {{provider}} /media',
    'xargs -l tar -cf out.tar -I {{provider}} /media',
    'xargs -e tar -cf out.tar -I {{provider}} /media',
])
def test_wrapper_option_arity_does_not_hide_the_child(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The wrapper value tables read a value option's long spelling the same way
# the short walk reads its letter, and the tables were missing them: a
# long name no value table held resolved as an unknown flag, so the value
# it reads landed in the child position and the program behind it went
# unscanned, with the child's -I carrier keeping its placeholder. The sets
# come from the installed tools' help: sudo's -h host entry, reported
# beyond the round before and confirmed by the review, with the longs of
# its other value options, xargs's --max-procs with the rest of its
# documented set, ionice's --class, stdbuf's --output, flock's --timeout
# beside the --wait the installed tool also accepts, runuser's --shell and
# --whitelist-environment, watch's --equexit and --shotsdir, and chrt's
# deadline parameters, each carrying its value so the child program keeps
# its position behind it. The last two rows hold the same boundary inside
# a find payload and behind a second wrapper.
@pytest.mark.parametrize('command', [
    'sudo -h host tar -cf out.tar -I {{provider}} /media',
    'sudo --user nobody tar -cf out.tar -I {{provider}} /media',
    'xargs --max-procs 2 tar -cf out.tar -I {{provider}} /media',
    'sudo --host host tar -cf out.tar -I {{provider}} /media',
    'sudo --close-from 5 tar -cf out.tar -I {{provider}} /media',
    'sudo --chdir /tmp tar -cf out.tar -I {{provider}} /media',
    'sudo --other-user nobody tar -cf out.tar -I {{provider}} /media',
    'ionice --class 3 tar -cf out.tar -I {{provider}} /media',
    'stdbuf --output 1M tar -cf out.tar -I {{provider}} /media',
    'xargs --max-args 2 tar -cf out.tar -I {{provider}} /media',
    'xargs --max-chars 4096 tar -cf out.tar -I {{provider}} /media',
    'xargs --process-slot-var SLOT tar -cf out.tar -I {{provider}} /media',
    'flock --timeout 5 /tmp/lock tar -cf out.tar -I {{provider}} /media',
    'runuser --shell /bin/sh nobody tar -cf out.tar -I {{provider}} /media',
    'runuser --whitelist-environment FOO nobody tar -cf out.tar -I {{provider}} /media',
    'watch --equexit 5 tar -cf out.tar -I {{provider}} /media',
    'watch --shotsdir /tmp/shots tar -cf out.tar -I {{provider}} /media',
    'chrt -d --sched-runtime 1e5 --sched-period 2e5 --sched-deadline 3e5 0 tar -cf '
    'out.tar -I {{provider}} /media',
    'find /media -exec sudo --user nobody tar -cf out.tar -I {{provider}} \\;',
    'nice -n5 xargs --max-procs 2 tar -cf out.tar -I {{provider}} /media',
])
def test_long_wrapper_option_values_do_not_hide_the_child(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The wrapper tables read a value option's short letter the way they read
# its long name, and the round before left the shorts of runuser's --shell
# and --whitelist-environment and watch's --equexit and --shotsdir off the
# tables, so each flag reading dropped its value into the child position
# and the program behind it went unscanned (sudo's list-only -U stays off
# by the review's ruling: the real sudo runs nothing behind it). GNU
# option parsing also accepts an unambiguous abbreviation of a long option
# name, so the long spellings now resolve the way the program tables
# resolve theirs: the exact name first, then an abbreviation exactly one
# documented long value spelling begins with (xargs's --max-pr is
# --max-procs, sudo's --use is --user), while a prefix more than one entry
# matches stays the unresolved name it is, the way a genuinely ambiguous
# prefix does. The rows hold the two named inputs, the short twins, one
# abbreviation per wrapper the tables name, and the last two hold the
# same boundary inside a find payload and behind a second wrapper.
@pytest.mark.parametrize('command', [
    'runuser -u nobody -w FOO tar -cf out.tar -I {{provider}} /media',
    'watch -x -q 5 tar -cf out.tar -I {{provider}} /media',
    'runuser -s /bin/sh nobody tar -cf out.tar -I {{provider}} /media',
    'watch -s /tmp/shots tar -cf out.tar -I {{provider}} /media',
    'watch -xq 5 tar -cf out.tar -I {{provider}} /media',
    'xargs --max-pr 2 tar -cf out.tar -I {{provider}} /media',
    'sudo --use nobody tar -cf out.tar -I {{provider}} /media',
    'flock --time 5 /tmp/lock tar -cf out.tar -I {{provider}} /media',
    'runuser --whitelist FOO nobody tar -cf out.tar -I {{provider}} /media',
    'watch --eq 5 tar -cf out.tar -I {{provider}} /media',
    'stdbuf --out 1M tar -cf out.tar -I {{provider}} /media',
    'chrt -d --sched-r 1e5 0 tar -cf out.tar -I {{provider}} /media',
    'timeout --kill 5 1 tar -cf out.tar -I {{provider}} /media',
    'ionice --classd 3 tar -cf out.tar -I {{provider}} /media',
    'su --whitelist FOO nobody tar -cf out.tar -I {{provider}} /media',
    'find /media -exec runuser -u nobody -w FOO tar -cf out.tar -I {{provider}} \\;',
    'nice -n5 xargs --max-pr 2 tar -cf out.tar -I {{provider}} /media',
])
def test_wrapper_short_and_abbreviated_options_do_not_hide_the_child(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# The generic carrier matching met a long spelling a program's documented
# vocabulary resolves before it resolved it: rsync's --exec is its
# documented --executability abbreviated, a flag reading nothing, but the
# cross-program exec match read it as the carrier and consumed the --rsh
# behind it as its payload, so the transport shell rsync runs its value
# through kept its placeholder. The documented reading now resolves ahead
# of the generic exact match the way it already resolved ahead of the
# carrier-prefix fallback, the same discipline the wrapper longs gained:
# the full name or the abbreviation exactly one documented entry resolves
# to, while a prefix more than one entry matches stays unresolved. The
# installed rsync rejects --exec outright (its popt does no long
# abbreviation), so the named input is a template the installed tool never
# runs, and the refusal still holds the model's reading: --executability
# is the flag --exec abbreviates for a GNU parser that does.
@pytest.mark.parametrize('command', [
    'rsync --exec --rsh {{provider}} src host:dest',
    'rsync --exec --rsync-path {{provider}} src host:dest',
    'find /media -exec rsync --exec --rsh {{provider}} src host:dest \\;',
])
def test_documented_long_reading_precedes_the_generic_carrier_match(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# watch assembles the command line it is given into a shell command string
# in its default mode (its help: -x passes the command to exec instead of
# "sh -c"), so a placeholder in it rides into shell text the lane refuses
# everywhere else, while -x or --exec hands the command line to execvp
# directly and the placeholders stay the plain arguments of a fixed
# program. The wrapper walk now requires the exec mode when the command
# watch runs carries a placeholder, at the top level, inside a find payload
# and behind a second wrapper.
@pytest.mark.parametrize('command', [
    'watch -n 5 /opt/pp.sh "{{provider}}"',
    'watch /opt/pp.sh "{{provider}}"',
    'find /media -exec watch -n 5 /opt/pp.sh "{{provider}}" \\;',
    'nice -n5 watch -n 5 /opt/pp.sh "{{provider}}"',
])
def test_watch_shell_assembly_with_placeholders_requires_exec_mode(command):
    with pytest.raises(ValueError, match='shell command text'):
        render(command)


# A wrapper's -- ends its own options the way a GNU parser ends them, but
# both walks kept reading the words behind it as the wrapper's own, so
# watch counted an -x sitting in the command text behind the separator as
# its exec mode and the round-22 gate let the shell-assembled placeholder
# through. The installed watch honors the separator (its header shows the
# words behind it as the command it runs at its default interval), and the
# walks now stop reading options and modes there: the words behind it fill
# the remaining positional operands and start the command among them, an
# option-looking word included, while an option that has read no value yet
# keeps the next word as its value the way the GNU parser does (nice -n --
# reads the adjustment --). Every separator-honoring tool the survey probed
# dies at an option-looking word in the command position, so the shapes the
# walks used to misread are templates the real tools never run, and the
# rows hold the mode spellings that changed a verdict: the named input,
# its long and abbreviated spellings, the separator behind real options,
# and the same boundary inside a find payload.
@pytest.mark.parametrize('command', [
    'watch -- -x /opt/pp.sh "{{provider}}"',
    'watch -- --exec /opt/pp.sh "{{provider}}"',
    'watch -- --ex /opt/pp.sh "{{provider}}"',
    'watch -n 5 -- -x /opt/pp.sh "{{provider}}"',
    'find /media -exec watch -- -x /opt/pp.sh "{{provider}}" \\;',
])
def test_wrapper_separator_words_are_not_wrapper_options(command):
    with pytest.raises(ValueError, match='shell command text'):
        render(command)


# chrt's deadline parameters read their nanosecond values behind their
# short letters the way they read them behind their long names, but the
# tables carried only the longs, so the walk read the installed tool's
# -T, -P and -D as unknown flags and their values as positional
# operands: the first value took the priority operand and the second
# became the program the wrapper runs, so the child's -I carrier kept
# its placeholder (chrt -d -T 1000000 -P 2000000 -D 2000000 0 tar
# -cf out.tar -I {{provider}}, where tar executes the program the -I
# value names). The installed chrt documents the three letters beside
# their longs and reads each as a required separate or attached value
# (a bare -T takes the word behind it: chrt names that word its
# runtime argument), so the letters sit beside their longs in the table.
# Regression rows cover the named input at the top level and the same
# boundary inside a find payload.
@pytest.mark.parametrize('command', [
    'chrt -d -T 1000000 -P 2000000 -D 2000000 0 tar -cf out.tar -I {{provider}} /media',
    'find /media -exec chrt -d -T 1000000 -P 2000000 -D 2000000 0 tar -cf out.tar -I {{provider}} /media \\;',
])
def test_chrt_deadline_shorts_do_not_hide_the_child(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


# A short option the arity tables leave to its generic reading, because
# its operand must parse as a value and an option-shaped word never
# parses as one (sort's -S size, -t separator character and -k key
# definition, tar's -b blocking factor), still reads that operand
# attached to its letter the way GNU getopt reads it, but the walk never
# recorded the attached value, so it treated the token as a letter still
# awaiting one and the bare -- behind it as that value: the option
# parsing the separator should have ended stayed live and the
# carrier-shaped filename behind it kept its refusal (sort -S1M --
# --compress-program={{provider}}, refused while the separate form
# -S 1M -- accepted, though the installed sort reads both the same way:
# the attached -S1M supplies the size, the separator ends the options and
# the last word is the filename sort cannot open, while a bare -S takes
# the -- as its value the GNU way, and tar's -b20 reads the same). The
# walk now records the attached operand the letter has read the way it
# records a documented data option's glued operand, so the bare -- behind
# the token is the separator it is and the words behind it keep their
# filename readings, at the top level and inside a find payload.
def test_attached_short_operands_keep_the_separator_behind_them():
    assert render('sort -S1M -- --compress-program={{provider}} /media') == [
        'sort', '-S1M', '--', '--compress-program=provider', '/media',
    ]
    assert render('sort -t: -- --compress-program={{provider}} /media') == [
        'sort', '-t:', '--', '--compress-program=provider', '/media',
    ]
    assert render('sort -k2 -- --compress-program={{provider}} /media') == [
        'sort', '-k2', '--', '--compress-program=provider', '/media',
    ]
    assert render('tar -b20 -- --checkpoint-action=exec={{release_info}} -cf out.tar /media') == [
        'tar', '-b20', '--', '--checkpoint-action=exec=release', '-cf', 'out.tar', '/media',
    ]
    assert render('find /media -exec sort -S1M -- --compress-program={{provider}} /media \\;') == [
        'find', '/media', '-exec', 'sort', '-S1M', '--', '--compress-program=provider', '/media',
        ';',
    ]


# The wrapper rules scan the child argv with the wrapped program's own
# facts, so the whole-argv scan consumes the validated child instead of
# rereading its interior with the wrapper's: tar's --label keeps reading
# the expression-looking token as its archive label behind nice, and
# watch's --exec is a flag naming the command it runs, so the validated
# child boundary holds even when the scan reads --exec with the
# cross-program rules and its value consumption covers the child program
# token, at the top level and inside a find payload.
def test_wrapper_child_keeps_its_own_program_facts():
    assert render('nice tar --label --expression={{release_info}} -cf /tmp/archive.tar /tmp/input') == [
        'nice', 'tar', '--label', '--expression=release', '-cf', '/tmp/archive.tar', '/tmp/input',
    ]
    assert render('find /media -exec nice tar --label --expression={{release_info}} '
                  '-cf /tmp/archive.tar /tmp/input \\;') == [
        'find', '/media', '-exec', 'nice', 'tar', '--label', '--expression=release',
        '-cf', '/tmp/archive.tar', '/tmp/input', ';',
    ]
    assert render('watch --exec sort -f "{{subtitles}}"') == [
        'watch', '--exec', 'sort', '-f', '/media/a show.en.srt',
    ]
    assert render('find /media -exec watch --exec sort -f "{{subtitles}}" \\;') == [
        'find', '/media', '-exec', 'watch', '--exec', 'sort', '-f', '/media/a show.en.srt', ';',
    ]


# The first word after a wrapper's own options and operands is the program
# it runs, and the words behind it are that program's own argv: an
# operand that names a wrapper of its own is data the child consumes,
# never a program the wrapper runs, so the validation stays with the real
# child and its transport option keeps its refusal, at the top level and
# inside a find payload.
@pytest.mark.parametrize('command', [
    'nice rsync nice tar -C --rsh={{provider}} src host:dest',
    'find /media -exec nice rsync nice tar -C --rsh={{provider}} src host:dest \\;',
])
def test_wrapper_child_operands_are_not_programs(command):
    with pytest.raises(ValueError, match='can carry executable code'):
        render(command)


def test_plain_data_option_values_stay_open():
    assert render('/opt/fixer --audio={{episode_language}} --series={{series_id}} "{{release_info}}"') == [
        '/opt/fixer', '--audio=English', '--series=12', 'release',
    ]
    # A carrier option with a fixed value keeps running too; only the
    # placeholder in its value is refused.
    assert render('tar --checkpoint-action=exec=/opt/fixed.sh "{{episode}}"') == [
        'tar', '--checkpoint-action=exec=/opt/fixed.sh', '/media/a show.mkv',
    ]


@pytest.mark.parametrize('command', [
    'sh -c "echo {{release_info}}"',
    'bash -c "echo {{release_info}}"',
    'ash -c "echo {{release_info}}"',
    'csh -c "echo {{release_info}}"',
    'tcsh -c "echo {{release_info}}"',
    'busybox ash -c "echo {{release_info}}"',
    'rbash -c "echo {{release_info}}"',
    'cmd /c "echo {{release_info}}"',
])
def test_placeholders_inside_shell_program_text_are_rejected(command):
    with pytest.raises(ValueError):
        render(command, release_info='; touch /tmp/never')


def test_static_shell_program_can_receive_metadata_as_positional_argument():
    assert render('sh -c \'printf "%s" "$1" | tee /tmp/log\' -- "{{release_info}}"',
                  release_info='; $(id)') == [
        'sh', '-c', 'printf "%s" "$1" | tee /tmp/log', '--', '; $(id)',
    ]


@pytest.mark.parametrize('template', [
    'env sh -c "echo {{release_info}}"',
    '/usr/bin/env bash -c "echo {{release_info}}"',
    'command sh -c "echo {{release_info}}"',
    'exec sh -c "echo {{release_info}}"',
    'python -c "print({{release_info}})"',
    'node --eval "console.log({{release_info}})"',
    'nodejs -e "console.log({{release_info}})"',
    'python -X dev -c "print({{release_info}})"',
    'node --require preload -e "console.log({{release_info}})"',
    'perl -I lib -e "print {{release_info}}"',
    'ruby -I lib -e "puts {{release_info}}"',
    'python -m {{provider}}',
    'powershell -EncodedCommand {{release_info}}',
    'bash --rcfile /fixed/rc -c "echo {{release_info}}"',
    'sh -o vi -c "echo {{release_info}}"',
    'python -X dev {{provider}}',
    'node --require fixed-module {{provider}}',
    'powershell -File {{provider}} {{release_info}}',
    'perl5.38.2 -e "print {{release_info}}"',
    'ruby3.4 -e "puts {{release_info}}"',
    'php8.3 -r "echo {{release_info}};"',
    'node20 -e "console.log({{release_info}})"',
    'sudo ruby3.4 -e "puts {{release_info}}"',
    'nice sh -c "echo {{release_info}}"',
    'timeout 60 bash -c "echo {{release_info}}"',
    '/usr/bin/nohup python3 -c "print({{release_info}})"',
    'busybox sh -c "echo {{release_info}}"',
    'nice -n 10 env sh -c "echo {{release_info}}"',
    'nice {{subtitles}}',
    'sudo {{subtitles}}',
    'busybox {{subtitles}}',
    'xargs {{subtitles}}',
    'nice -n {{score}} /opt/scripts/postprocess.sh "{{subtitles}}"',
    # A wrapper's option values and positional operands are data it consumes,
    # not the program it runs, so a placeholder may not hide among them and
    # then take the program position.
    'sudo -u nobody {{release_info}} {{subtitles}}',
    'timeout 60 {{provider}}',
    'nice -n 10 {{provider}}',
    'flock /tmp/lock {{provider}}',
    'taskset 0x1 {{provider}}',
    'gosu nobody {{provider}}',
    'watch -n 5 {{provider}}',
    'sudo awk "{{release_info}}" /tmp/input',
    # su runs the text after -c through the target account's shell, so
    # provider-supplied metadata there is shell code, not data.
    'su nobody -s /bin/sh -c "echo {{release_info}}"',
    'su root -c {{release_info}}',
    'su -c "echo {{release_info}}"',
    "su nobody -c'echo {{release_info}}'",
])
def test_dynamic_code_wrappers_and_interpreters_are_rejected(template):
    with pytest.raises(ValueError):
        render(template, release_info='; touch /tmp/never')


def test_fixed_interpreter_script_after_option_operand_can_receive_data():
    assert render('python -X dev /opt/scripts/postprocess.py "{{release_info}}"',
                  release_info='; $(id)') == [
        'python', '-X', 'dev', '/opt/scripts/postprocess.py', '; $(id)',
    ]


@pytest.mark.parametrize('template, expected', [
    ('nice -n 10 /opt/scripts/postprocess.sh "{{release_info}}"',
     ['nice', '-n', '10', '/opt/scripts/postprocess.sh', '; $(id)']),
    ('timeout 60 python3 /opt/scripts/postprocess.py "{{release_info}}"',
     ['timeout', '60', 'python3', '/opt/scripts/postprocess.py', '; $(id)']),
    ('perl5.38.2 /opt/scripts/postprocess.pl "{{release_info}}"',
     ['perl5.38.2', '/opt/scripts/postprocess.pl', '; $(id)']),
    # The operands sudo, taskset and watch consume before the program are not
    # the program, so the fixed program still sits in the program position.
    ('sudo -u nobody /opt/pp.sh "{{subtitles}}"',
     ['sudo', '-u', 'nobody', '/opt/pp.sh', '/media/a show.en.srt']),
    ('taskset -c 0-3 /opt/pp.sh "{{subtitles}}"',
     ['taskset', '-c', '0-3', '/opt/pp.sh', '/media/a show.en.srt']),
    ('taskset 0x1 /opt/pp.sh "{{subtitles}}"',
     ['taskset', '0x1', '/opt/pp.sh', '/media/a show.en.srt']),
    ('watch -x -n 5 /opt/pp.sh "{{subtitles}}"',
     ['watch', '-x', '-n', '5', '/opt/pp.sh', '/media/a show.en.srt']),
    # -x and --exec are watch flags, not options taking a value: the fixed
    # program still sits in the program position behind them.
    ('watch -x /opt/pp.sh "{{subtitles}}"',
     ['watch', '-x', '/opt/pp.sh', '/media/a show.en.srt']),
    # su without -c runs the fixed program as the named user, with no shell
    # command text in the template.
    ('su nobody /opt/pp.sh "{{subtitles}}"',
     ['su', 'nobody', '/opt/pp.sh', '/media/a show.en.srt']),
])
def test_wrapped_fixed_program_can_receive_data(template, expected):
    assert render(template, release_info='; $(id)') == expected


def test_raw_command_string_is_not_passed_to_popen():
    with mock.patch('subtitles.post_processing.subprocess.Popen') as popen:
        _postprocessing_locked('process --unsafe', '/media/a show.mkv')
    popen.assert_not_called()


def test_expanded_argv_is_passed_to_popen_without_shell():
    child = mock.MagicMock()
    child.communicate.return_value = ('', '')
    with mock.patch('subtitles.post_processing.subprocess.Popen', return_value=child) as popen:
        _postprocessing_locked(['process', '--name=a & b'], '/media/a show.mkv')
    assert popen.call_args.args[0] == ['process', '--name=a & b']
    assert popen.call_args.kwargs['shell'] is False


class _FakeUpdate:
    def values(self, **_kwargs):
        return self


@pytest.fixture
def settings_save(monkeypatch):
    """save_settings with its persistence side effects stubbed out."""
    from app import config

    saved = []
    rows = []

    class _FakeSelect:
        def where(self, *_conditions):
            return self

    class _FakeRows:
        # False like the None the stub used to return, so a query whose result
        # is only checked for truthiness keeps behaving as before.
        def __bool__(self):
            return False

        def all(self):
            return rows

    monkeypatch.setattr(config, 'write_config', lambda: saved.append(
        (config.settings.general.use_postprocessing, config.settings.general.postprocessing_cmd)) or True)
    monkeypatch.setattr(config, 'validate_log_regex', lambda: None)
    monkeypatch.setitem(sys.modules, 'app.database', SimpleNamespace(
        database=SimpleNamespace(execute=lambda _statement: _FakeRows()),
        update=lambda _model: _FakeUpdate(), System=object,
        select=lambda *columns: _FakeSelect(),
        TableArrInstances=SimpleNamespace(name='name', options='options', enabled='enabled'),
    ))
    monkeypatch.setattr(settings.general, 'use_postprocessing', True)
    monkeypatch.setattr(settings.general, 'postprocessing_cmd', '/scripts/pp.sh {{subtitles}}')
    return config, saved, rows


@pytest.mark.parametrize('items', [
    [('settings-general-postprocessing_cmd', ['/scripts/pp.sh {{subtitles}} | tee /tmp/log'])],
    [('settings-general-use_postprocessing', ['true']),
     ('settings-general-postprocessing_cmd', ['/scripts/pp.sh {{subtitles}} && other'])],
])
def test_saving_a_shell_command_is_refused_with_a_clear_message(settings_save, items):
    config, saved, _ = settings_save

    with pytest.raises(ValidationError, match='without a shell'):
        config.save_settings(items)

    assert saved == []
    assert settings.general.postprocessing_cmd == '/scripts/pp.sh {{subtitles}}'


def test_turning_post_processing_on_checks_the_stored_command(settings_save, monkeypatch):
    config, saved, _ = settings_save
    monkeypatch.setattr(settings.general, 'use_postprocessing', False)
    monkeypatch.setattr(settings.general, 'postprocessing_cmd', '/scripts/pp.sh {{subtitles}} > /tmp/log')

    with pytest.raises(ValidationError, match='without a shell'):
        config.save_settings([('settings-general-use_postprocessing', ['true'])])

    assert saved == []
    assert settings.general.use_postprocessing is False


def test_turning_post_processing_on_with_a_blank_stored_command_is_refused(settings_save, monkeypatch):
    """A blank stored command cannot run, so turning the toggle on has to name
    it rather than switch post-processing on to nothing."""
    config, saved, _ = settings_save
    monkeypatch.setattr(settings.general, 'use_postprocessing', False)
    monkeypatch.setattr(settings.general, 'postprocessing_cmd', '')

    with pytest.raises(ValidationError, match='fixed executable path'):
        config.save_settings([('settings-general-use_postprocessing', ['true'])])

    assert saved == []


def test_turning_post_processing_on_checks_the_stored_instance_commands(settings_save):
    """An own command stored while the instance toggle was off becomes effective
    the moment the global switch is turned on, so that save validates each
    stored own command instead of letting a broken one out of dormancy."""
    config, saved, rows = settings_save
    rows.append(SimpleNamespace(
        name='Anime',
        options=json.dumps({'subtitle_settings': {'general': {'postprocessing_cmd': '/pp.sh {{subtitles}} | tee'}}})))

    with pytest.raises(ValidationError, match='Post-processing command for Anime'):
        config.save_settings([('settings-general-use_postprocessing', ['true'])])

    assert saved == []


def test_enabling_post_processing_with_a_command_still_checks_the_stored_instance_commands(settings_save):
    """The stored instance commands become effective with the global switch
    whether or not the same request also sets a global command, so the toggle
    in the request decides the instance checks, not the absence of a command.
    An instance command that only ran shell-free while the global toggle was
    off must not slip into an enabled configuration behind a valid new one."""
    config, saved, rows = settings_save
    rows.append(SimpleNamespace(
        name='Anime',
        options=json.dumps({'subtitle_settings': {'general': {'postprocessing_cmd': '/pp.sh {{subtitles}} | tee'}}})))

    with pytest.raises(ValidationError, match='Post-processing command for Anime'):
        config.save_settings([('settings-general-use_postprocessing', ['true']),
                              ('settings-general-postprocessing_cmd', ['/scripts/pp.sh "{{subtitles}}"'])])

    assert saved == []


def test_turning_post_processing_on_accepts_valid_stored_instance_commands(settings_save):
    config, saved, rows = settings_save
    rows.append(SimpleNamespace(
        name='Anime',
        options=json.dumps({'subtitle_settings': {'general': {'postprocessing_cmd': '/pp.sh "{{subtitles}}"'}}})))

    config.save_settings([('settings-general-use_postprocessing', ['true'])])

    assert len(saved) == 1


def test_a_submitted_command_is_checked_even_when_post_processing_is_off(settings_save, monkeypatch):
    """An enabled instance can inherit the global command while the global
    toggle is off, and a command that cannot run should not be stored at all,
    so a save that sets the command is refused whatever the toggle says."""
    config, saved, _ = settings_save
    monkeypatch.setattr(settings.general, 'use_postprocessing', False)

    with pytest.raises(ValidationError, match='without a shell'):
        config.save_settings([('settings-general-postprocessing_cmd',
                               ['/scripts/pp.sh {{subtitles}} | tee /tmp/log'])])

    assert saved == []
    assert settings.general.use_postprocessing is False


@pytest.mark.parametrize('items, expected', [
    # A stored command from before the rule never blocks unrelated saves or switching it off.
    ([('settings-general-page_size', ['50'])], (True, '/scripts/pp.sh {{subtitles}} | tee /tmp/log')),
    ([('settings-general-use_postprocessing', ['false'])], (False, '/scripts/pp.sh {{subtitles}} | tee /tmp/log')),
    ([('settings-general-postprocessing_cmd', ['/scripts/pp.sh "{{subtitles}}" "a | b"'])],
     (True, '/scripts/pp.sh "{{subtitles}}" "a | b"')),
])
def test_valid_or_unrelated_saves_are_accepted(settings_save, monkeypatch, items, expected):
    config, saved, _ = settings_save
    monkeypatch.setattr(settings.general, 'postprocessing_cmd', '/scripts/pp.sh {{subtitles}} | tee /tmp/log')

    config.save_settings(items)

    assert saved == [expected]


def test_instance_override_with_shell_syntax_is_refused():
    from arr_instances.subtitle_settings import validate_subtitle_settings

    with pytest.raises(ValueError, match='without a shell'):
        validate_subtitle_settings({'general': {'use_postprocessing': True,
                                                'postprocessing_cmd': '/pp.sh {{subtitles}} | tee'}})
    assert validate_subtitle_settings({'general': {'postprocessing_cmd': '/pp.sh "{{subtitles}}"'}}) == {
        'general': {'postprocessing_cmd': '/pp.sh "{{subtitles}}"'}}
    # A disabled override does not run, so its stored command is left alone.
    validate_subtitle_settings({'general': {'use_postprocessing': False,
                                            'postprocessing_cmd': '/pp.sh {{subtitles}} | tee'}})


def test_stored_shell_commands_are_health_issues(migration_engine, monkeypatch):  # noqa: F811
    from sqlalchemy.orm import sessionmaker
    from app.database import Base, TableArrInstances
    from utilities import health

    Base.metadata.create_all(migration_engine)
    blob = lambda general: json.dumps({'subtitle_settings': {'general': general}})  # noqa: E731
    with sessionmaker(bind=migration_engine)() as session:
        session.add_all([
            TableArrInstances(id=1, kind='sonarr', stable_key='a', name='Anime', port=1, enabled=1,
                              options=blob({'postprocessing_cmd': '/pp.sh {{subtitles}} && x'})),
            TableArrInstances(id=2, kind='radarr', stable_key='b', name='Off', port=1, enabled=1,
                              options=blob({'use_postprocessing': False,
                                            'postprocessing_cmd': '/pp.sh {{subtitles}} | x'})),
            TableArrInstances(id=3, kind='radarr', stable_key='c', name='Disabled', port=1, enabled=0,
                              options=blob({'postprocessing_cmd': '/pp.sh {{subtitles}} | x'})),
            TableArrInstances(id=4, kind='radarr', stable_key='d', name='Fine', port=1, enabled=1,
                              options=blob({'postprocessing_cmd': '/pp.sh "{{subtitles}}"'})),
            TableArrInstances(id=5, kind='radarr', stable_key='e', name='Inherits', port=1, enabled=1),
            TableArrInstances(id=6, kind='sonarr', stable_key='f', name='Same', port=1, enabled=1,
                              options=blob({'postprocessing_cmd': '/pp.sh {{subtitles}} && x'})),
        ])
        session.commit()
        monkeypatch.setattr(health, 'database', session)
        for switch in ('use_sonarr', 'use_radarr', 'use_sportarr'):
            monkeypatch.setattr(settings.general, switch, False)
        monkeypatch.setattr(settings.general, 'use_postprocessing', True)
        monkeypatch.setattr(settings.general, 'postprocessing_cmd', '/pp.sh {{subtitles}} > /tmp/log')

        issues = [item for item in health.get_health_issues() if 'Post-processing' in item['object']]

    assert [item['object'] for item in issues] == [
        'Post-processing command', 'Post-processing command for Anime',
        'Post-processing command for Same']
    assert all('without a shell' in item['issue'] for item in issues)


def test_blank_enabled_commands_are_health_issues(migration_engine, monkeypatch):  # noqa: F811
    from sqlalchemy.orm import sessionmaker
    from app.database import Base, TableArrInstances
    from utilities import health

    Base.metadata.create_all(migration_engine)
    blob = lambda general: json.dumps({'subtitle_settings': {'general': general}})  # noqa: E731
    with sessionmaker(bind=migration_engine)() as session:
        session.add_all([
            TableArrInstances(id=1, kind='sonarr', stable_key='a', name='Own blank', port=1, enabled=1,
                              options=blob({'use_postprocessing': True, 'postprocessing_cmd': '  '})),
            TableArrInstances(id=2, kind='radarr', stable_key='b', name='Off blank', port=1, enabled=1,
                              options=blob({'use_postprocessing': False, 'postprocessing_cmd': ''})),
            TableArrInstances(id=3, kind='radarr', stable_key='c', name='Inherits blank', port=1, enabled=1,
                              options=blob({'use_postprocessing': True})),
        ])
        session.commit()
        monkeypatch.setattr(health, 'database', session)
        for switch in ('use_sonarr', 'use_radarr', 'use_sportarr'):
            monkeypatch.setattr(settings.general, switch, False)
        monkeypatch.setattr(settings.general, 'use_postprocessing', True)
        monkeypatch.setattr(settings.general, 'postprocessing_cmd', '')

        issues = [item for item in health.get_health_issues() if 'Post-processing' in item['object']]

    # The instance inheriting the blank global command is already named by the
    # global line, and one line is enough for it.
    assert issues == [
        {'object': 'Post-processing command', 'issue': 'Post-processing is on with no command'},
        {'object': 'Post-processing command for Own blank', 'issue': 'Post-processing is on with no command'},
    ]
