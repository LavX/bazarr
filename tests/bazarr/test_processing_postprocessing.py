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
    assert render('"/opt/my tools/process" --file={{episode}} '
                  '"prefix {{subtitles}} suffix" --score={{score}}') == [
        '/opt/my tools/process', '--file=/media/a show.mkv',
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
    argv = render(command + ' --file={{episode}} "{{release_info}}" "Name: {{release_info}}" '
                         '"{{series_id}}" "{{episode_id}}"',
                  episode=episode, release_info=payload, series_id='', episode_id='')

    _postprocessing_locked(argv, episode)

    assert json.loads(output.read_text(encoding='utf-8')) == [
        '--file=' + episode, payload, 'Name: ' + payload, '', '',
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
    ('watch -n 5 /opt/pp.sh "{{subtitles}}"',
     ['watch', '-n', '5', '/opt/pp.sh', '/media/a show.en.srt']),
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
