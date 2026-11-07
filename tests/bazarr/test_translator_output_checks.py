"""Output checks on an engine's answer, and the right-to-left marks Bazarr+ adds."""
import pytest

from subtitles.tools.translate.runtime.output_checks import (
    ABSOLUTE_LENGTH_LIMIT, MIN_LENGTH_LIMIT, PDF, RLE, add_rtl_marks, check_line, check_output, length_limit)


def codes(source, output):
    return check_output(source, output).codes()


def test_a_clean_answer_passes():
    source = ["Hello there.", "<i>Who are you?</i>", "", "Line one\nline two"]
    output = ["Szia.", "<i>Ki vagy te?</i>", "", "Egyes sor\nkettes sor"]
    result = check_output(source, output)
    assert result.ok and result.violations == ()


def test_the_line_count_must_match():
    assert codes(["a", "b"], ["x"]) == {"line_count"}
    assert codes(["a"], ["x", "y"]) == {"line_count"}
    assert codes([], []) == set()
    # A count mismatch is reported once; the lines are not compared against each other.
    assert len(check_output(["a", "b", "c"], ["x"]).violations) == 1


def test_a_line_with_text_cannot_come_back_empty():
    assert codes(["Hello"], [""]) == {"empty"}
    assert codes(["Hello"], ["   "]) == {"empty"}
    assert codes(["  "], [""]) == set()  # a blank stays blank


def test_a_line_must_stay_within_its_length_bound():
    assert length_limit("hi") == MIN_LENGTH_LIMIT
    assert length_limit("x" * 100) == 400
    assert length_limit("x" * 5000) == ABSOLUTE_LENGTH_LIMIT
    assert codes(["hi"], ["x" * MIN_LENGTH_LIMIT]) == set()
    assert codes(["hi"], ["x" * (MIN_LENGTH_LIMIT + 1)]) == {"too_long"}
    assert codes(["x" * 100], ["y" * 400]) == set()
    assert codes(["x" * 100], ["y" * 401]) == {"too_long"}
    assert codes(["x" * 5000], ["y" * 5001]) == {"too_long"}


@pytest.mark.parametrize("tag", ["<i>", "</i>", "<b>", "</B>", "<U>", "<s>", "</S>"])
def test_the_four_subtitle_tags_are_allowed(tag):
    assert codes(["a"], [f"{tag}x{tag}"]) == set()


@pytest.mark.parametrize("bad", [
    "<font color=red>x</font>", "<script>alert(1)</script>", "<b class=\"x\">x</b>", "<i >x</i>", "<br>",
    "<a href=x>x</a>", "<!-- x -->", "<?php x ?>", "<img src=x>", "<ii>x</ii>", "<b/>",
])
def test_markup_beyond_the_four_tags_is_refused(bad):
    assert "disallowed_tag" in codes(["a"], [bad])


@pytest.mark.parametrize("text", ["a < b", "a > b", "I <3 you", "5 < 6 > 4", "<", ">", "< b >", "x << y"])
def test_text_that_only_looks_like_markup_is_fine(text):
    assert codes(["a"], [text]) == set()


@pytest.mark.parametrize("char", ["\x00", "\x07", "\x08", "\t", "\r", "\x1b", "\x7f", "\x85", " ", " ",
                                  "‪", "‫", "‬", "‭", "‮", "⁦", "⁧", "⁨",
                                  "⁩"])
def test_control_characters_and_direction_overrides_are_refused(char):
    assert "control_character" in codes(["a"], [f"ab{char}cd"])


def test_a_line_break_and_the_direction_marks_a_translator_may_use_are_fine():
    assert codes(["a\nb"], ["x\ny"]) == set()
    assert codes(["a"], ["‏x‎"]) == set()  # right-to-left and left-to-right marks
    assert codes(["a"], ["؜x"]) == set()  # Arabic letter mark
    assert codes(["a"], ["x​y"]) == set()


def test_a_non_text_line_is_refused():
    assert [v.code for v in check_line("a", None)] == ["not_text"]
    assert codes(["a"], [None]) == {"not_text"}


def test_every_violation_names_its_line():
    result = check_output(["a", "b", "c"], ["ok", "", "x\x00y"])
    assert sorted((v.code, v.index) for v in result.violations) == [("control_character", 2), ("empty", 1)]
    assert not result.ok


def test_several_problems_on_one_line_are_all_reported():
    assert codes(["a"], ["<font>\x00" + "x" * 300]) == {"disallowed_tag", "control_character", "too_long"}


# ---------------------------------------------------------------- right-to-left marks

def test_a_left_to_right_language_is_left_alone():
    lines = ["Hello", "", "two\nlines"]
    assert add_rtl_marks(lines, False) == lines


def test_each_line_of_a_right_to_left_cue_is_wrapped_once():
    assert add_rtl_marks(["שלום", "", "אחד\nשניים"], True) == [
        f"{RLE}שלום{PDF}", "", f"{RLE}אחד{PDF}\n{RLE}שניים{PDF}"]


def test_adding_the_marks_twice_changes_nothing():
    once = add_rtl_marks(["שלום", "אחד\nשניים", f"{RLE}מוכן"], True)
    assert add_rtl_marks(once, True) == once
    assert once[2] == f"{RLE}מוכן{PDF}"


def test_the_marks_survive_the_output_check_only_after_the_check():
    """Bazarr+ adds the marks after checking, so a marked line is refused if an engine sent it that way."""
    marked = add_rtl_marks(["שלום"], True)
    assert codes(["Hello"], ["שלום"]) == set()
    assert codes(["Hello"], marked) == {"control_character"}


def test_a_blank_line_gets_no_marks():
    assert add_rtl_marks(["", "  ", "\n"], True) == ["", "  ", "\n"]
