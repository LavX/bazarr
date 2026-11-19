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
# * plain cues must produce output byte-identical to origin/development, and every
#   output must be a string: a load failure fails the gate instead of comparing as equal;
# * a tagged cue must produce, byte for byte, development's plain output with each
#   surviving line wrapped in the tags its own input line had (route-normalised where
#   the file route cannot keep a tag form, see _expected_wrapper);
# * dialogue that development keeps in plain form is never removed when tagged.
#
# Two routes reach the mod, and they hand it different text:
#
# * "file": SubtitleModifications.load(fn=...) parses the SRT with pysubs2, which turns
#   <i> into {\i1} / {\i0}, drops unknown tags such as <font> and <I>, and writes nested
#   pairs in its own form.
# * "content": SubtitleModifications.load(content=...), the call that
#   Subtitle.get_modified_content makes (it adds fix_text, which the gate leaves out).
#   It keeps literal HTML tags, so tags come back exactly as they went in.
import json
import os
import re
import subprocess
import sys
from itertools import combinations
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
# Production runs Remove HI next to the other line mods (bazarr/subtitles/tools/mods.py).
COMBINED_MODS = (["OCR_fixes", "common", "remove_HI"], ["OCR_fixes", "common", "remove_HI(keep_lyrics=1)"])
MIN_PLAIN_CUES = 300
MIN_TAGGED_COMPARISONS = 3000


# --- running the mod -------------------------------------------------------
def _run(route, srt, mods, workdir):
    """Output of the mod for one SRT on one route, or None when the file does not load.
    The gate treats None as a failure, never as a match. _DEV_RUNNER runs the same code."""
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
# The runner loads the reference package from its own directory in a separate interpreter,
# so the reference never shares a module name with the package under test.
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

def safe(route, srt, mods, workdir):
    # An exception is reported per job, so the gate can name the cue that raised it.
    try:
        return run(route, srt, mods, workdir)
    except Exception as exc:
        return {"error": "%s: %s" % (type(exc).__name__, exc)}

jobs = json.load(sys.stdin)
workdir = tempfile.mkdtemp()
out = [[safe(route, srt, mods, workdir) for route in ("content", "file")] for srt, mods in jobs]
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
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", _DEV_RUNNER, str(libs)],
                          input=json.dumps(jobs), capture_output=True, text=True, check=True, env=env)
    return json.loads(proc.stdout)


def _current_outputs(jobs, tmp_path):
    def safe(route, srt, mods):
        try:
            return _run(route, srt, mods, tmp_path)
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
    return [[safe(route, srt, mods) for route in ROUTES] for srt, mods in jobs]


def _assert_all_loaded(outputs, jobs, label):
    """A gate comparison is only meaningful when both sides produced an output. A failure
    names the cue, the route and the mods, and carries the error the mod raised."""
    for (srt, mods), pair in zip(jobs, outputs):
        for route, out in zip(ROUTES, pair):
            if not isinstance(out, str):
                reason = out.get("error", "the mod did not load this cue") if isinstance(out, dict) else out
                raise AssertionError("%s: %s on the %s route (mods %r):\n%s" % (
                    label, reason, route, mods, srt))


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
_UNKNOWN_CUES = [
    ["{\\an8}♪ la la ♪"], ["Hello <i>there</i>"], ["<i>a <b>b</b> c</i>"],
    ["<i>♪ Music ♪", "<font color=\"#fff\">Hello</font></i>"],
    ["<i>[door creaks]", "I know you're in there (knocking)</i>"], ["<i>♪ la", "</i>"],
    ["<i>unclosed"], ["<i>a</i> <i>b</i>"], ["<i></i>"], ["<i>   </i>"], ["<b></b>", "Hallo."],
    # The lone closing-tag merge on development's own path: this used to raise IndexError.
    ["Hello", "{\\i1}-{\\i0}{\\i0}", "{\\i1}-{\\i0}{\\i0}"],
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


_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$", re.MULTILINE)


def _is_real_srt(text):
    """A timestamped SRT with no format placeholders in it."""
    return bool(_TIMESTAMP.search(text)) and not re.search(r"%\d*[sdr]|\{:", text)


def _is_plain(srt):
    return "<" not in srt and "{\\" not in srt


def _source_srts():
    """SRT blobs written in the existing HI tests, taken from the test sources themselves.
    Format templates are not SRT and are left out here, by the timestamp check."""
    import ast
    blobs = []
    for name in ("tests/subliminal_patch/test_mods.py", "tests/subliminal_patch/test_mods_keep_lyrics.py",
                 "tests/bazarr/test_subzero_keep_lyrics.py", "tests/bazarr/test_subzero_hearing_impaired.py"):
        tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
        blobs.extend(node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str) and "-->" in node.value)
    return [blob for blob in blobs if _is_real_srt(blob) and _is_plain(blob)]


# A whole file of upper-case lines, so that the uppercase count can reach its threshold.
_CAPS_FILE = _srt_from_cues([["WE ARE GOING NOW THE PLAN IS ON [door creaks]"] for _ in range(12)])
_WRAPPED_CAPS_FILE = _srt_from_cues(
    [["<font color=\"#ffff00\">WE ARE GOING NOW THE PLAN IS ON</font>"] for _ in range(12)])
# Markup that is declined (text after the wrap), on a file that reaches the threshold. Plain
# upper-case dialogue is in the same file, so the uppercase flag changes what the rules do.
_UNKNOWN_FILE = _srt_from_cues(
    [["<font color=\"#ffff00\">WE ARE GOING NOW THE PLAN IS ON</font> [door creaks]"] for _ in range(12)]
    + [["OPEN THE DOOR"] for _ in range(12)])

PLAIN_SRTS = ([_srt_from_cues(cue) for cue in _plain_cues()] + [_CAPS_FILE] + _source_srts())


def _cue_count(srt):
    return len(_TIMESTAMP.findall(srt))


# --- the plain gate ----------------------------------------------------------
def test_plain_corpus_is_byte_identical_to_development(development_libs, tmp_path):
    assert all(_is_real_srt(srt) and _is_plain(srt) for srt in PLAIN_SRTS)
    compared = sum(_cue_count(srt) for srt in PLAIN_SRTS)
    assert compared >= MIN_PLAIN_CUES, "only %d real cues in the corpus" % compared
    jobs = [(srt, mods) for srt in PLAIN_SRTS for mods in MODS + COMBINED_MODS[:1]]
    jobs += [(srt, COMBINED_MODS[1]) for srt in PLAIN_SRTS[:50]]
    dev = _development_outputs(development_libs, jobs)
    current = _current_outputs(jobs, tmp_path)
    _assert_all_loaded(dev, jobs, "development")
    _assert_all_loaded(current, jobs, "current")
    differences = []
    for (srt, mods), dev_pair, cur_pair in zip(jobs, dev, current):
        for route, dev_out, cur_out in zip(ROUTES, dev_pair, cur_pair):
            if dev_out != cur_out:
                differences.append((route, mods, srt, dev_out, cur_out))
    assert not differences, "%d plain outputs differ from development, first: %r" % (
        len(differences), differences[:1])


def test_unknown_markup_is_left_to_development(development_libs, tmp_path):
    jobs = [(_pair_srt(lines), mods) for lines in _UNKNOWN_CUES for mods in MODS + COMBINED_MODS[:1]]
    jobs += [(_UNKNOWN_FILE, mods) for mods in MODS]
    dev = _development_outputs(development_libs, jobs)
    current = _current_outputs(jobs, tmp_path)
    _assert_all_loaded(dev, jobs, "development")
    _assert_all_loaded(current, jobs, "current")
    assert dev == current


# --- the tagged gate ----------------------------------------------------------
# Wrappers that tag one whole line. The SSA one is override tags on the file route and
# literal text on the content route, which is the point of a variant on both.
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


def _expected_wrapper(wrapper, route):
    """(opening, closing, exact) for a wrapper as the route writes it back.

    The content route writes HTML back as it came, and SSA as HTML. The file route reads
    <I> and <font> as unknown tags and drops them. Its SRT writer keeps italic and underline
    only, so bold is written as plain text, and nested pairs come out in its own form. A
    nested wrapper is therefore checked for visible text and balance only (exact False)."""
    opening, closing = wrapper
    if opening.startswith("{\\"):
        name = opening[2].lower()
        return "<%s>" % name, "</%s>" % name, True
    if route == "content":
        return opening, closing, True
    if opening in ("<i>", "<u>"):
        return opening, closing, True
    if opening == "<i><b>":
        return opening, closing, False
    return "", "", True


_OWN_TIMESTAMP = "00:00:02,000 --> 00:00:02,500"


def _cue_block(out):
    lines = out.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == _OWN_TIMESTAMP:
            end = index + 1
            while end < len(lines) and lines[end].strip():
                end += 1
            return lines, index, end
    return None


def _cue_text(out):
    block = _cue_block(out)
    if block is None:
        return None
    lines, index, end = block
    return lines[index + 1:end]


def _replace_cue_text(out, new_lines):
    lines, index, end = _cue_block(out)
    return "\n".join(lines[:index + 1] + new_lines + lines[end:])


def _candidates(wrappers, dev_text):
    """Every way the surviving lines of a cue can carry their input lines' tags, in order.
    Development only removes lines and changes their text, so each output line is the
    development text of one line wrapped in the tags of that same input line."""
    for positions in combinations(range(len(wrappers)), len(dev_text)):
        yield [wrappers[k][0] + text + wrappers[k][1] for text, k in zip(dev_text, positions)]


def _tagged_comparison(cue_lines, rotation, mods, dev_pair, cur_pair):
    """Check one tagged cue on both routes. Returns how many routes were compared exactly."""
    exact_count = 0
    for route, dev_out, cur_out in zip(ROUTES, dev_pair, cur_pair):
        wrappers = [_expected_wrapper(WRAPPERS[(k + rotation) % len(WRAPPERS)], route)
                    for k in range(len(cue_lines))]
        label = "cue %r, rotation %d, route %s, mods %r" % (cue_lines, rotation, route, mods)
        if all(exact for _o, _c, exact in wrappers):
            dev_text = _cue_text(dev_out)
            if dev_text is None:
                expected = [dev_out]
            else:
                expected = [_replace_cue_text(dev_out, candidate)
                            for candidate in _candidates(wrappers, dev_text)]
            assert cur_out in expected, "%s\nactual:\n%s\nexpected one of:\n%s" % (
                label, cur_out, "\n---\n".join(expected))
            exact_count += 1
        else:
            assert _visible_lines(cur_out) == _visible_lines(dev_out), label
            assert all(_balanced(cur_out, name) for name in ("i", "b", "u")), label
    return exact_count


def test_tagged_corpus_matches_development_exactly(development_libs, tmp_path):
    cues = _plain_cues()
    specs = [(lines, rotation, MODS[0]) for lines in cues for rotation in range(len(WRAPPERS))]
    specs += [(lines, rotation, COMBINED_MODS[0]) for lines in cues for rotation in (0, 3)]
    plain_keys = sorted({(tuple(lines), tuple(mods)) for lines, _r, mods in specs})
    plain_jobs = [(_pair_srt(list(lines)), list(mods)) for lines, mods in plain_keys]
    tagged_jobs = [(_pair_srt(lines, rotation), mods) for lines, rotation, mods in specs]
    dev = dict(zip(plain_keys, _development_outputs(development_libs, plain_jobs)))
    current = _current_outputs(tagged_jobs, tmp_path)
    _assert_all_loaded(list(dev.values()), plain_jobs, "development")
    _assert_all_loaded(current, tagged_jobs, "current")
    exact = 0
    for (lines, rotation, mods), cur_pair in zip(specs, current):
        dev_pair = dev[(tuple(lines), tuple(mods))]
        exact += _tagged_comparison(lines, rotation, mods, dev_pair, cur_pair)
    assert exact >= MIN_TAGGED_COMPARISONS, "only %d exact comparisons" % exact


def test_all_caps_wrapped_file_matches_development_plain(development_libs, tmp_path):
    # Accepted markup is plain text to the uppercase count, so a file of wrapped caps
    # must read exactly as its plain form does.
    plain = _CAPS_FILE.replace(" [door creaks]", "")
    tagged = _WRAPPED_CAPS_FILE
    jobs = [(plain, ["remove_HI"]), (tagged, ["remove_HI"])]
    dev = _development_outputs(development_libs, jobs)
    current = _current_outputs(jobs, tmp_path)
    _assert_all_loaded(dev, jobs, "development")
    _assert_all_loaded(current, jobs, "current")
    for route, dev_out, cur_out in zip(ROUTES, dev[0], current[1]):
        assert _visible_lines(cur_out) == _visible_lines(dev[0][ROUTES.index(route)]), route


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
        cases.extend((line, _pair_srt([line], rotation)) for rotation in range(len(WRAPPERS)))
    plain_jobs = [(_pair_srt([line]), ["remove_HI"]) for line in DIALOGUE_TABLE]
    plain_dev = _development_outputs(development_libs, plain_jobs)
    for line, dev_pair in zip(DIALOGUE_TABLE, plain_dev):
        assert line in _visible(dev_pair[0]), "development removes %r in plain form" % line
    jobs = [(srt, ["remove_HI"]) for _line, srt in cases]
    current = _current_outputs(jobs, tmp_path)
    for (line, srt), cur_pair in zip(cases, current):
        for route, cur_out in zip(ROUTES, cur_pair):
            assert line in _visible(cur_out), (route, srt, cur_out)


# --- explicit tagged behaviour ----------------------------------------------------
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
def test_tagged_multiline_cue_with_declined_markup_keeps_dialogue(cue, dialogue, render):
    # These cues are not whole-line wraps, so they reach development's rules unchanged.
    out = render(_cue(cue), ["remove_HI"])
    assert dialogue in _visible(out)
    assert "Hallo." in out


def test_declined_bracket_across_lines_keeps_dialogue_on_content_route():
    # Content route only. On the file route the SSA form of this cue is removed whole by
    # development's full-bracket rule, the same as its plain form (ticket 602).
    srt = _cue("<i>[door creaks]\nI know you're in there (knocking)</i>")
    sm = SubtitleModifications()
    assert sm.load(content=srt, language=Language("eng"), mods=["remove_HI"])
    sm.modify("remove_HI")
    assert "I know you're in there" in _visible(sm.f.to_string("srt"))


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


def test_detect_uppercase_does_not_count_wrapped_markup_letters():
    # Wrapped lines are plain text to the count, so the letters of <font color=...> do not count.
    sm = SubtitleModifications()
    assert sm.load(content=_WRAPPED_CAPS_FILE, language=Language("eng"))
    assert sm.detect_uppercase() is True


def test_case_detection_names_select_development_processors_in_order():
    from subzero.modification import registry
    from subzero.modification.main import CASE_DETECTION_PROCESSORS

    assert CASE_DETECTION_PROCESSORS == (
        "HI_brackets_full", "HI_before_colon_caps", "HI_before_colon_noncaps", "HI_brackets")
    processors = registry.mods["remove_HI"].processors
    assert [p.name for p in processors[:4]] == list(CASE_DETECTION_PROCESSORS)
    assert [p.name for p in processors if p.name in CASE_DETECTION_PROCESSORS] == list(CASE_DETECTION_PROCESSORS)


# --- the markup layer on its own ------------------------------------------------
@pytest.mark.parametrize("text, plain, opens, closes", [
    ("<i>♪ MTV ♪</i>", "♪ MTV ♪", ["<i>"], ["</i>"]),
    ("{\\i1}♪ MTV ♪{\\i0}", "♪ MTV ♪", ["{\\i1}"], ["{\\i0}"]),
    ("<i><b>x</b></i>", "x", ["<i><b>"], ["</b></i>"]),
    ("<i>♪ la</i>\\N<i>Hello</i>", "♪ la\\NHello", ["<i>", "<i>"], ["</i>", "</i>"]),
    ("Hello\\N\t\\N<i>x</i>", "Hello\\N\t\\Nx", ["", "", "<i>"], ["", "", "</i>"]),
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
    "<i></i>",
    "<i>   </i>",
    "plain text only",
])
def test_unwrap_entry_declines_what_it_does_not_understand(text):
    assert unwrap_entry(text) is None


def test_rewrap_wraps_each_line_in_its_own_tags():
    wrapped = unwrap_entry('<i>a</i>\\N<b>b</b>\\N<font color="#fff">c</font>')
    assert wrapped.plain == "a\\Nb\\Nc"
    assert wrapped.rewrap(["a", "b", "c"], [0, 1, 2]) == ["<i>a</i>", "<b>b</b>", '<font color="#fff">c</font>']
    assert wrapped.rewrap(["a", "c"], [0, 2]) == ["<i>a</i>", '<font color="#fff">c</font>']


def test_rewrap_keeps_nested_pairs_in_order():
    assert unwrap_entry("<i><b>x</b></i>").rewrap(["x"], [0]) == ["<i><b>x</b></i>"]


def test_rewrap_refuses_misaligned_origins():
    wrapped = unwrap_entry("<i>a</i>")
    with pytest.raises(ValueError):
        wrapped.rewrap(["a"], [0, 0])
