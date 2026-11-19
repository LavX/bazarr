# coding=utf-8
# Remove HI mod: markup layer, and the differential gate against origin/development.
# See https://github.com/LavX/bazarr/issues/828
#
# The gate runs against a frozen copy of origin/development's Remove HI implementation,
# vendored under tests/bazarr/fixtures/hi_reference and checked by hash.
#
# The rules work on plain text exactly as origin/development has them. Only cues whose
# markup is simple (whole-line pairs of <i>, <b>, <u>, <font> or SSA override tags) are
# handed to the rules with that markup removed, and wrapped again afterwards. So:
#
# * plain cues must produce output byte-identical to origin/development;
# * a tagged cue must show the same visible text as its plain form does on
#   origin/development, minus any orphaned tags;
# * dialogue that development keeps in plain form is never removed when tagged.
#
# Two routes reach the mod, and they hand it different text:
#
# * "file": SubtitleModifications.load(fn=...) parses the SRT with pysubs2, which turns
#   <i> into {\i1} / {\i0} and drops unknown tags such as <font>.
# * "content": SubtitleModifications.load(content=...), the call subtitles_apply_mods
#   reaches through Subtitle.get_modified_content. It keeps literal HTML tags.
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from subliminal_patch import Subtitle
from subzero.language import Language
from subzero.modification.main import SubtitleModifications
from subzero.modification.markup import unwrap_entry

REPO = Path(__file__).resolve().parents[2]
# The reference: origin/development's Remove HI implementation, vendored and frozen.
REFERENCE_LIBS = Path(__file__).resolve().parent / "fixtures" / "hi_reference"
REFERENCE_SOURCE_SHA = "3dc435b8f4f6aadbe3cae9236b12b0e4acf9ce01"
# SHA-256 of each vendored file, as recorded when it was copied. A test checks them, so an
# edit to the reference shows up as a failure rather than as a silent change of the gate.
REFERENCE_SHA256 = {
    "subzero/__init__.py": "1aa9fcfd6358fd51f63e99d3e3b81d499b77b24862ff8752d03aab09578e894d",
    "subzero/language.py": "1347df8df6125efec57089daa522112cb188870d524dd93cd420c6c0c4e6f413",
    "subzero/modification/__init__.py": "19a55556b883419450f06f1454fef1cf6615d558dcf391a79246795003e46c07",
    "subzero/modification/exc.py": "e7b3d1e3b07d8cab1deaccd920ffc3f73dcbccd382d4b9a65a477678ddf07c15",
    "subzero/modification/main.py": "668610f6d2ba24864b6e7f460cce2fa7d0ee998b60bd6141446e5be710ec2ba6",
    "subzero/modification/registry.py": "00d4691b0d6229f6bdbf4e0bddc846dcc85306ee9e9191da3d8060ac193c66bb",
    "subzero/modification/dictionaries/__init__.py": "3a3cbccf72329138952acfd374b5af3e60d693443bc558b6205ffa0a87cb87dc",
    "subzero/modification/dictionaries/data.py": "3259320bdb9bf08fd05d6675ce8155cd71b57440af0b004a41ea74d5f51a547f",
    "subzero/modification/mods/__init__.py": "ac6e865a47748189c1f234669fb509d5a93a5f1642bf199a14a678ce849232b2",
    "subzero/modification/mods/color.py": "c25275c781e48be806817f34938635f008aa58b6d4bb5b63bd9ad01ff2c82ed2",
    "subzero/modification/mods/common.py": "af976c33a6f5c252f2fe313c75b4ca22f0b12ab388e1f7790846913e3ae21cff",
    "subzero/modification/mods/emoji.py": "1d5332d2e1936286b9c23212d5bad8927fa66965f6b7d7395e70388c846839ef",
    "subzero/modification/mods/fps.py": "c62965e51db5ac92973282908927d50b7f995249f509c680e2d8c7ceefbcba29",
    "subzero/modification/mods/hearing_impaired.py": "14282dae9304ce935f09cc05129938b72e8dae46ce6c41cc44334f0bc143ff16",
    "subzero/modification/mods/ocr_fixes.py": "97786e27134d6851934139b7db6110c72f4cb3ed0718beb51ed67cd0e96847ca",
    "subzero/modification/mods/offset.py": "ca574d4e011e1518c9b3befb09b48fd0fd276fa7a1b662c8bd584a2ae3b9f7d1",
    "subzero/modification/mods/two_point_fit.py": "ca284f13cb124752ebe7c099809c9f6051745c57c128919442d8d49cd97d53bc",
    "subzero/modification/processors/__init__.py": "e99532a23efd2efaa3e50ca732b7ab420f3c7bb38e3f3374bcd8b449a1177cf1",
    "subzero/modification/processors/re_processor.py": "ef38d3b643d85e4767886d97aa86a121888a810d7d57671f37f6f89b48ab2858",
    "subzero/modification/processors/string_processor.py": "7007527032943d0a4d5826da9f5c4966caf1c07bb5158ca22fdf802223b9fc5d",
}
ROUTES = ("content", "file")
MODS = (["remove_HI"], ["remove_HI(keep_lyrics=1)"])


# --- running the mod -------------------------------------------------------
def _run(route, srt, mods, workdir):
    """Output of the mod for one SRT on one route, or None when the file does not load.
    This is the same code the development runner in _DEV_RUNNER executes."""
    sm = SubtitleModifications()
    if route == "content":
        loaded = sm.load(content=srt, language=Language("eng"), mods=mods)
    else:
        path = Path(workdir) / "movie.en.srt"
        path.write_bytes(srt.encode("utf-8"))
        loaded = sm.load(fn=str(path), language=Language("eng"), mods=mods)
    if not loaded:
        return None
    sm.modify(*mods)
    return sm.f.to_string("srt")


def _render_file(srt, mods, tmp_path):
    out = _run("file", srt, mods, tmp_path)
    assert out is not None
    return out


def _render_content_production(srt, mods, tmp_path):
    # Mirror the production call in custom_libs/subliminal_patch/subtitle.py.
    sub = Subtitle(Language("eng"), mods=mods, original_format=True)
    sub.content = srt.encode("utf-8")
    assert sub.is_valid()
    content = sub.get_modified_content(format="srt")
    return content.decode("utf-8") if content else ""


_RENDERERS = {"file": _render_file, "content": _render_content_production}


@pytest.fixture(params=["file", "content"])
def render(request, tmp_path):
    renderer = _RENDERERS[request.param]
    return lambda srt, mods: renderer(srt, mods, tmp_path)


KEEP_CUE = "3\n00:00:05,000 --> 00:00:06,000\nHallo.\n\n"


def _cue(text):
    return "1\n00:00:01,000 --> 00:00:02,000\n{}\n\n".format(text) + KEEP_CUE


def _visible(out):
    """The text a viewer sees: markup (HTML or SSA override tags) removed."""
    return re.sub(r"<[^>]+>|\{\\[^}]*\}", "", out)


def _visible_lines(out):
    return [line.strip() for line in _visible(out).splitlines() if line.strip()]


def _balanced(out, name):
    return out.count("<%s>" % name) == out.count("</%s>" % name)


# --- development, side by side ---------------------------------------------
# The runner loads origin/development's subzero package from its own directory, so the
# two trees never share a module name in one interpreter.
_DEV_RUNNER = r'''
import json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from subzero.language import Language
from subzero.modification.main import SubtitleModifications

def run(route, srt, mods, workdir):
    sm = SubtitleModifications()
    if route == "content":
        loaded = sm.load(content=srt, language=Language("eng"), mods=mods)
    else:
        path = Path(workdir) / "movie.en.srt"
        path.write_bytes(srt.encode("utf-8"))
        loaded = sm.load(fn=str(path), language=Language("eng"), mods=mods)
    if not loaded:
        return None
    sm.modify(*mods)
    return sm.f.to_string("srt")

jobs = json.load(sys.stdin)
workdir = tempfile.mkdtemp()
out = [[run(route, srt, mods, workdir) for route in ("content", "file")] for srt, mods in jobs]
json.dump(out, sys.stdout)
'''


@pytest.fixture
def development_libs():
    return REFERENCE_LIBS


def test_vendored_reference_is_unchanged():
    import hashlib
    assert REFERENCE_SOURCE_SHA
    recorded = dict(REFERENCE_SHA256)
    on_disk = {}
    for path in sorted((REFERENCE_LIBS / "subzero").rglob("*.py")):
        rel = path.relative_to(REFERENCE_LIBS).as_posix()
        on_disk[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert on_disk == recorded, "the vendored reference changed: %r" % sorted(
        k for k in set(on_disk) | set(recorded) if on_disk.get(k) != recorded.get(k))
    assert all(path.exists() for path in (REFERENCE_LIBS / rel for rel in recorded))


def _development_outputs(libs, jobs):
    # A separate interpreter loads the reference, so its package never shares a name with
    # the package under test. No bytecode is written into the fixture tree.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", _DEV_RUNNER, str(libs)],
                          input=json.dumps(jobs), capture_output=True, text=True, check=True, env=env)
    return json.loads(proc.stdout)


def _current_outputs(jobs, tmp_path):
    return [[_run(route, srt, mods, tmp_path) for route in ROUTES] for srt, mods in jobs]


# --- corpus ----------------------------------------------------------------
_MUSIC = [
    "♪ la la la ♪", "♪ MTV", "la la ♪", "♪♪", "♫ la ♫", "¶ la ¶", "- ♪ la ♪",
    "♪ la la", "[singing] ♪ Happy birthday ♪", "MAN: ♪ la ♪", "♪♪ la la ♪♪",
    "# la la la #", "* la la *", "#@$%!", "*sighs*", "- #1 is the best!",
]
_BRACKETS = [
    "[door creaks]", "(sighs)", "[sighs] Hello there.", "He said [quietly] go",
    "[door-creaks] Hello there.", "[one] and [two] are here.", "[radio chatter]",
]
_LABELS = ["MAN: Hello.", "JOHN: Where is it?", "Hello there."]
_CAPS = ["WE ARE GOING NOW", "OPEN THE DOOR", "LAUGHING", "What the f***?"]
_DIALOGUE = [
    "Hallo.", "Where is #7 now?", "That's *my* car!", "We're #1!", "Room #204, please.",
    "#1 fan!", "*Never*", "Press * to continue.", "Ten * twenty = 200.",
]
# Multi-line cues the reviewers raised, plain form.
_REVIEW_MULTILINE = [
    "♪ la ♪\nHello there.", "[door creaks]\nI know you're in there (knocking)",
    "[DRAMATIC ORCHESTRAL\nMUSIC PLAYING]", "(INDISTINCT CHATTER\nOVER RADIO)", "(SIGHS)\n(LAUGHS)",
    "♪ When the night has come\nAnd the land is dark", "[radio chatter]\nAll units respond.",
    "♪ la la ♪\nWe're #1!", "♪ Music ♪ Hello there", "# la la\nla la #", "*sighs*\nHello",
]
# Markup the layer does not take apart. Each must come out exactly as development makes it.
_UNKNOWN_MARKUP = [
    "{\\an8}♪ la la ♪", "Hello <i>there</i>", "<i>a <b>b</b> c</i>", "<i>♪ Music ♪\n<font color=\"#fff\">Hello</font></i>",
    "<i>[door creaks]\nI know you're in there (knocking)</i>", "<i>♪ la\n</i>", "<i>unclosed", "<i>a</i> <i>b</i>",
]
_SINGLES = _MUSIC + _BRACKETS + _LABELS + _CAPS + _DIALOGUE


def _plain_pairs(count, seed):
    import random
    rng = random.Random(seed)
    return [(rng.choice(_SINGLES), rng.choice(_SINGLES)) for _ in range(count)]


def _plain_cues():
    """Every plain cue of the corpus, each as its list of lines."""
    cues = [[text] for text in _SINGLES] + [text.split("\n") for text in _REVIEW_MULTILINE]
    cues += [[first, second] for first, second in _plain_pairs(300, 20260619)]
    return cues


def _srt_from_cues(cue_lines_list):
    blocks = []
    for index, lines in enumerate(cue_lines_list, 1):
        start = index * 2
        blocks.append("%d\n00:00:%02d,000 --> 00:00:%02d,500\n%s\n" % (index, start, start, "\n".join(lines)))
    return "\n".join(blocks)


def _source_srts():
    """SRT blobs written in the existing HI tests, taken from the test sources themselves."""
    import ast
    blobs = []
    for name in ("tests/subliminal_patch/test_mods.py", "tests/subliminal_patch/test_mods_keep_lyrics.py",
                 "tests/bazarr/test_subzero_keep_lyrics.py", "tests/bazarr/test_subzero_hearing_impaired.py"):
        tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
        blobs.extend(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str) and "-->" in node.value)
    return blobs


def _is_plain(srt):
    return "<" not in srt and "{\\" not in srt


PLAIN_SRTS = [_srt_from_cues(cue) for cue in _plain_cues()] + [b for b in _source_srts() if _is_plain(b)]

# Wrappers that tag one whole line. The SSA one is plain text to the literal HTML route
# and override tags to the file route, which is the point of a variant on both.
WRAPPERS = [
    ("<i>", "</i>"), ("<b>", "</b>"), ('<font color="#ffff00">', "</font>"),
    ("<i><b>", "</b></i>"), ("<I>", "</I>"), ("{\\i1}", "{\\i0}"), ("<u>", "</u>"),
]


def _wrap_lines(lines, rotation):
    out = []
    for index, line in enumerate(lines):
        opening, closing = WRAPPERS[(index + rotation) % len(WRAPPERS)]
        out.append(opening + line + closing)
    return out


def _pair_srt(lines, rotation=None):
    """Two cues: the cue under test (plain, or tagged with a rotation), then dialogue."""
    first = lines if rotation is None else _wrap_lines(lines, rotation)
    return _srt_from_cues([first, ["Hallo."]])


# --- the gate --------------------------------------------------------------
def test_plain_corpus_is_byte_identical_to_development(development_libs, tmp_path):
    assert len(PLAIN_SRTS) >= 300
    assert all(_is_plain(srt) for srt in PLAIN_SRTS)
    jobs = [(srt, mods) for srt in PLAIN_SRTS for mods in MODS]
    dev = _development_outputs(development_libs, jobs)
    current = _current_outputs(jobs, tmp_path)
    differences = []
    for (srt, mods), dev_pair, cur_pair in zip(jobs, dev, current):
        for route, dev_out, cur_out in zip(ROUTES, dev_pair, cur_pair):
            if dev_out != cur_out:
                differences.append((route, mods, srt, dev_out, cur_out))
    assert not differences, "%d plain outputs differ from development, first: %r" % (
        len(differences), differences[:1])


def test_tagged_corpus_shows_development_plain_text(development_libs, tmp_path):
    cues = _plain_cues()
    plain_jobs = [(_pair_srt(lines), ["remove_HI"]) for lines in cues]
    dev = _development_outputs(development_libs, plain_jobs)
    differences = []
    for rotation in (0, 1):
        tagged_jobs = [(_pair_srt(lines, rotation), ["remove_HI"]) for lines in cues]
        current = _current_outputs(tagged_jobs, tmp_path)
        for (srt, _mods), dev_pair, cur_pair in zip(tagged_jobs, dev, current):
            for route, dev_out, cur_out in zip(ROUTES, dev_pair, cur_pair):
                if _visible_lines(dev_out) != _visible_lines(cur_out):
                    differences.append((rotation, route, srt, dev_out, cur_out))
                elif not all(_balanced(cur_out, name) for name in ("i", "b", "u")):
                    differences.append((rotation, route, srt, dev_out, cur_out))
    assert not differences, "%d tagged cues differ from their development plain text, first: %r" % (
        len(differences), differences[:1])


# Realistic dialogue. Each line must survive tagged, and development must keep it in plain
# form too: the test asserts that first, so the table only pins what development keeps.
DIALOGUE_TABLE = [
    "Where is #7 now?", "Hello there, friend.", "That's *my* car!", "We're #1 in the league.",
    "What the f**k is that?", "He said # is a hashtag.", "Press * to continue.", "Stop! Now!",
    "Room #204, please.", "Ten * twenty = 200.", "Yes, #1 is correct.", "Get out of my *house*!",
    "Call me on 555 #2.", "I'm not * ready yet.", "He is very #1 in class.", "It's all # fine.",
    "Hey, *you* there!", "Sold #5!", "Two * three # four", "Hi.", "Hallo.",
]


def test_dialogue_table_survives_tagged_and_plain(development_libs, tmp_path):
    cases = []
    for line in DIALOGUE_TABLE:
        cases.append((line, _pair_srt([line])))
        cases.extend((line, _pair_srt([line], rotation)) for rotation in (0, 1, 2))
    plain_jobs = [(_pair_srt([line]), ["remove_HI"]) for line in DIALOGUE_TABLE]
    plain_dev = _development_outputs(development_libs, plain_jobs)
    for line, dev_pair in zip(DIALOGUE_TABLE, plain_dev):
        assert line in _visible(dev_pair[0]), "development removes %r in plain form" % line
    jobs = [(srt, ["remove_HI"]) for _line, srt in cases]
    current = _current_outputs(jobs, tmp_path)
    for (line, srt), cur_pair in zip(cases, current):
        for route, cur_out in zip(ROUTES, cur_pair):
            assert line in _visible(cur_out), (route, srt, cur_out)


def test_unknown_markup_is_left_to_development(development_libs, tmp_path):
    jobs = [(_pair_srt([text]), mods) for text in _UNKNOWN_MARKUP for mods in MODS]
    dev = _development_outputs(development_libs, jobs)
    current = _current_outputs(jobs, tmp_path)
    assert dev == current


# --- explicit tagged behaviour that must match development's plain behaviour -----------
@pytest.mark.parametrize("cue", [
    "<i>♪ MTV ♪</i>",
    '<font color="#ff0000">♪ MTV ♪</font>',
    "<b>♪ MTV ♪</b>",
    "<I>♪ MTV ♪</I>",
    "<i><font color=\"#ffff00\">♪ la ♪</font></i>",
    "<b><i>♪ la ♪</i></b>",
    "<i><b>♪♪</b></i>",
    "{\\i1}♪ Some lyric{\\i0}",
    "<i>♪ Some lyric</i>",
])
def test_wrapped_music_line_is_removed(cue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert "♪" not in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue", [
    "<i>[door creaks]</i>",
    "<i><font color=\"#ffff00\">[door creaks]</font></i>",
])
def test_wrapped_bracket_line_is_removed_whole(cue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert "door creaks" not in out
    assert "<i" not in out
    assert "</i>" not in out
    assert "Hallo." in out


def test_keep_lyrics_keeps_wrapped_lyric(render):
    out = render(_cue("<i>♪ la la ♪</i>"), ["remove_HI(keep_lyrics=1)"])
    assert "♪ la la ♪" in _visible(out)
    assert "Hallo." in out


@pytest.mark.parametrize("cue", [
    "<i>♪ ~ ♪</i>",
    "<i>- ♪♪</i>",
    "<font color=\"#ffffff\">♪ ♪</font>",
])
def test_keep_lyrics_drops_wrapped_decoration(cue, render):
    out = render(_cue(cue), ["remove_HI(keep_lyrics=1)"])
    assert "♪" not in out
    assert "Hallo." in out


def test_keep_lyrics_keeps_unpaired_tagged_music_line_on_content_route():
    # An opening tag whose closing tag is on another line is not a whole-line wrap, so the
    # layer leaves the cue to development's rules, which keep it.
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n<i>♪♪\nHello.</i>\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nHallo.\n"
    )
    sm = SubtitleModifications()
    assert sm.load(content=srt, language=Language("eng"), mods=["remove_HI(keep_lyrics=1)"])
    sm.modify("remove_HI(keep_lyrics=1)")
    out = sm.f.to_string("srt")
    assert "<i>♪♪" in out
    assert "Hello.</i>" in out
    assert "Hallo." in out


@pytest.mark.parametrize("cue, line", [
    ("<i>He said # is a symbol</i>", "He said # is a symbol"),
    ("He said <i>#</i> is a symbol", "He said # is a symbol"),
    ("<font color=\"#ffffff\">Hello there.</font>", "Hello there."),
])
def test_remove_hi_keeps_tagged_text_with_hash(cue, line, render):
    out = render(_cue(cue), ["remove_HI"])
    assert line in _visible(out)
    assert "Hallo." in out


@pytest.mark.parametrize("cue, dialogue", [
    ("<i>♪ Music ♪\n<font color=\"#ffffff\">Hello there.</font></i>", "Hello there."),
    ("<i>♪ Music ♪\nWe're #1!</i>", "We're #1!"),
])
def test_tagged_multiline_cue_keeps_its_dialogue(cue, dialogue, render):
    out = render(_cue(cue), ["remove_HI"])
    assert dialogue in _visible(out)
    assert "Hallo." in out


# --- detect_uppercase ------------------------------------------------------
def _detect_uppercase(srt, tmp_path):
    path = tmp_path / "movie.en.srt"
    path.write_bytes(srt.encode("utf-8"))
    sm = SubtitleModifications()
    assert sm.load(fn=str(path), language=Language("eng"))
    return sm.detect_uppercase()


def test_detect_uppercase_counts_all_caps_file_with_lowercase_bracket_cues(tmp_path):
    entries = "".join(
        "{}\n00:00:{:02d},000 --> 00:00:{:02d},000\nWE ARE GOING NOW THE PLAN IS ON [door creaks]\n\n".format(
            i, i, i)
        for i in range(1, 11)
    )
    assert _detect_uppercase(entries, tmp_path) is True


def test_detect_uppercase_rejects_lowercase_dialogue(tmp_path):
    entries = "".join(
        "{}\n00:00:{:02d},000 --> 00:00:{:02d},000\nwe are going now the plan is on [door creaks]\n\n".format(
            i, i, i)
        for i in range(1, 11)
    )
    assert _detect_uppercase(entries, tmp_path) is False


def test_detect_uppercase_ignores_markup_letters():
    # Content route: the production load keeps the tags, pysubs2 would drop <font>.
    entries = "".join(
        "{}\n00:00:{:02d},000 --> 00:00:{:02d},000\n"
        "<font color=\"#ffff00\">WE ARE GOING NOW THE PLAN IS ON</font> [door creaks]\n\n".format(i, i, i)
        for i in range(1, 11)
    )
    sm = SubtitleModifications()
    assert sm.load(content=entries, language=Language("eng"))
    assert sm.detect_uppercase() is True


def test_case_detection_names_all_resolve_to_processors():
    from subzero.modification import registry
    from subzero.modification.main import CASE_DETECTION_PROCESSORS

    names = {p.name for p in registry.mods["remove_HI"].processors}
    assert set(CASE_DETECTION_PROCESSORS) <= names


# --- the markup layer on its own ------------------------------------------------
@pytest.mark.parametrize("text, plain, opens, closes", [
    ("<i>♪ MTV ♪</i>", "♪ MTV ♪", ["<i>"], ["</i>"]),
    ("{\\i1}♪ MTV ♪{\\i0}", "♪ MTV ♪", ["{\\i1}"], ["{\\i0}"]),
    ("<i><b>x</b></i>", "x", ["<i><b>"], ["</b></i>"]),
    ("<i>♪ la</i>\\N<i>Hello</i>", "♪ la\\NHello", ["<i>", "<i>"], ["</i>", "</i>"]),
])
def test_unwrap_entry_takes_simple_whole_line_pairs_apart(text, plain, opens, closes):
    wrapped = unwrap_entry(text)
    assert wrapped is not None
    assert wrapped.plain == plain
    assert wrapped.opens == opens
    assert wrapped.closes == closes


@pytest.mark.parametrize("text", [
    "Hello <i>there</i>",
    "<i>a</i> <i>b</i>",
    "<i>unclosed",
    "<i>♪ Music ♪\\N<font color=\"#fff\">Hello</font></i>",
    "<i>a <b>b</b> c</i>",
    "<i>{\\an8}x</i>",
    "plain text only",
])
def test_unwrap_entry_declines_what_it_does_not_understand(text):
    assert unwrap_entry(text) is None
