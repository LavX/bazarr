# coding=utf-8


from subtitles.tools.combine.rules import (
    SourcePaths,
    resolve_source_paths,
)


def make_video_dir(tmp_path, files):
    """Create empty subtitle files in tmp_path so resolve can find them."""
    for name in files:
        (tmp_path / name).write_text("")
    return tmp_path


def test_finds_sources_in_relative_subfolder(tmp_path, monkeypatch):
    # Video alongside the .mkv, but the SRTs live in a configured subfolder.
    (tmp_path / "Movie.mkv").write_text("")
    subs = tmp_path / "Subs"
    subs.mkdir()
    (subs / "Movie.en.srt").write_text("")
    (subs / "Movie.hu.srt").write_text("")
    monkeypatch.setattr(
        "subtitles.tools.combine.naming.external_subtitles_search_dirs",
        lambda video_path: [str(tmp_path), str(subs)],
    )
    result = resolve_source_paths(
        video_path=str(tmp_path / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result is not None
    assert result.primary == str(subs / "Movie.en.srt")
    assert result.secondaries == [str(subs / "Movie.hu.srt")]


def test_all_sources_present(tmp_path):
    base = make_video_dir(tmp_path, ["Movie.en.srt", "Movie.hu.srt", "Movie.mkv"])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert isinstance(result, SourcePaths)
    assert result.primary == str(base / "Movie.en.srt")
    assert result.secondaries == [str(base / "Movie.hu.srt")]


def test_missing_source_returns_none(tmp_path):
    base = make_video_dir(tmp_path, ["Movie.en.srt", "Movie.mkv"])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result is None


def test_skips_combined_and_modifier_files(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.hu.srt",
        "Movie.en.combined-hu.srt",
        "Movie.en.hi.srt",
        "Movie.en.forced.srt",
        # Sync-engine outputs from PR 158 must not be picked as combine sources.
        "Movie.en.ffsubsync.srt",
        "Movie.en.autosubsync.srt",
        "Movie.en.alass.srt",
        "Movie.hu.ffsubsync.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.srt")
    assert result.secondaries == [str(base / "Movie.hu.srt")]


def test_three_languages(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv", "Movie.de.srt", "Movie.es.srt", "Movie.zh.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["de", "es", "zh"],
    )
    assert result.primary == str(base / "Movie.de.srt")
    assert result.secondaries == [
        str(base / "Movie.es.srt"),
        str(base / "Movie.zh.srt"),
    ]


def test_picks_hi_when_only_hi_exists(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.hi.srt",
        "Movie.hu.hi.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.hi.srt")
    assert result.secondaries == [str(base / "Movie.hu.hi.srt")]


def test_prefers_plain_over_hi_when_both_exist(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.en.hi.srt",
        "Movie.hu.hi.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.srt")
    assert result.secondaries == [str(base / "Movie.hu.hi.srt")]


def test_prefers_hi_over_forced(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.forced.srt",
        "Movie.en.hi.srt",
        "Movie.hu.forced.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result.primary == str(base / "Movie.en.hi.srt")
    assert result.secondaries == [str(base / "Movie.hu.forced.srt")]


def test_skips_chained_modifier_sync_outputs(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.hi.ffsubsync.srt",
        "Movie.hu.hi.srt",
    ])
    # en.hi.ffsubsync is a sync output of an HI subtitle; not a valid source.
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    )
    assert result is None


def test_maps_zh_tw_filename_to_zt_code(tmp_path):
    """Bazarr writes Traditional Chinese as .zh-TW.srt; profiles request zt."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zh-TW.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


def test_maps_lowercase_zh_tw_alias(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-tw.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zh-tw.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


def test_maps_zht_alias(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zht.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zht.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


def test_maps_pt_br_filename_to_pb_code(tmp_path):
    """Same hyphen-tag bug class as zh-TW (Brazilian Portuguese)."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.pt-BR.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["pb", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.pt-BR.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


# --- Combined outputs are never sources -------------------------------------


def test_combined_outputs_are_never_sources(tmp_path):
    """A combined artifact must never be fed back in as a source.

    Combine is pure composition: re-composing an already combined file would
    duplicate every cue. Both the ".combined-" guard and the modifier whitelist
    reject these names, so this stays green if either one alone is loosened.
    """
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.srt",
        "Movie.zt.combined-en.srt",
        "Movie.en.combined-zt.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zh-TW.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


def test_combined_output_is_not_a_source_when_the_plain_file_is_gone(tmp_path):
    """With no plain zt file on disk, the combined artifact is the only zt-ish
    candidate. Picking it up would rebuild a combine from its own output, so the
    rule must skip instead."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zt.combined-en.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    ) is None


# --- The video's own name must not supply the language tag ------------------


def test_video_name_does_not_supply_the_language_tag(tmp_path):
    """'Elephant' ends in 'hant', a Traditional Chinese filename tag, so a
    whole-filename substring match reads Elephant.hi.srt as Chinese HI. It is
    Hindi, and it must resolve as Hindi."""
    base = make_video_dir(tmp_path, [
        "Elephant.mkv",
        "Elephant.hi.srt",
        "Elephant.en.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Elephant.mkv"),
        languages=["hi", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Elephant.hi.srt")
    assert result.secondaries == [str(base / "Elephant.en.srt")]


def test_video_name_does_not_offer_a_hindi_file_as_chinese(tmp_path):
    """The other half of the same defect: mis-reading the Hindi file as zt
    would compose a Chinese combine out of a Hindi subtitle. Skip instead."""
    base = make_video_dir(tmp_path, [
        "Elephant.mkv",
        "Elephant.hi.srt",
        "Elephant.en.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Elephant.mkv"),
        languages=["zt", "en"],
    ) is None


# --- Plain 2-letter codes win over the custom-tag lists ---------------------


def test_plain_two_letter_code_is_not_shadowed_by_a_custom_tag(tmp_path):
    """'.sc' is Sardinian, an ISO 639-1 code Bazarr seeds, and it is also in
    CustomLanguage's Simplified Chinese disambiguation list. The strict parser
    owns 2-letter codes, so Movie.sc.srt is Sardinian."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.sc.srt",
        "Movie.en.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["sc", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.sc.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


def test_sardinian_file_is_not_offered_as_chinese(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.sc.srt",
        "Movie.en.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zh", "en"],
    ) is None


# --- Custom tags keep the hi / forced variants ------------------------------


def test_maps_zh_tw_forced_filename_to_zt(tmp_path):
    """The flagship tag with the forced modifier. CustomLanguage's own forced
    lookup uses a 12-character window that '.zh-tw.forced' overruns, so the
    modifier has to be read off the isolated segment instead."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.forced.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zh-TW.forced.srt")
    assert result.secondaries == [str(base / "Movie.en.srt")]


def test_maps_zh_tw_hi_filename_to_zt(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.hi.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zh-TW.hi.srt")


def test_maps_pt_br_forced_filename_to_pb(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.pt-BR.forced.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["pb", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.pt-BR.forced.srt")


def test_prefers_plain_custom_tag_over_hi_and_forced(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.srt",
        "Movie.zh-TW.hi.srt",
        "Movie.zh-TW.forced.srt",
    ])
    result = resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    )
    assert result is not None
    assert result.primary == str(base / "Movie.zh-TW.srt")


# --- Segment structure still constrains custom tags -------------------------


def test_rejects_two_language_segments(tmp_path):
    """Movie.en.pt.srt is not a Portuguese subtitle. 'pt' sits in the modifier
    slot, and the modifier slot only accepts hi / sdh / cc / forced."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.en.pt.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "pt"],
    ) is None


def test_rejects_extra_segments_before_a_custom_tag(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.foo.bar.zh-TW.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    ) is None


def test_rejects_a_third_segment_after_a_modifier(tmp_path):
    """Only <tag> or <tag>.<modifier> is a name combine understands. A third
    segment means another tool's artifact (here sushi, an audio-based retimer),
    and combine cannot know what it holds, so it skips rather than guess."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.hu.srt",
        "Movie.en.hi.sushi.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["en", "hu"],
    ) is None


def test_sibling_edition_custom_tag_is_not_a_source(tmp_path):
    """The glob only prefix-tests the base name, so 'Movie.Extended.zh-TW.srt'
    turns up while resolving 'Movie.mkv'. It belongs to the other edition, and
    accepting it would compose the wrong video's subtitle."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.Extended.mkv",
        "Movie.en.srt",
        "Movie.Extended.zh-TW.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    ) is None


def test_skips_sync_engine_output_of_a_custom_tag(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.alass.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    ) is None


def test_skips_sync_engine_output_of_a_custom_tag_with_modifier(tmp_path):
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.tc.hi.alass.srt",
    ])
    assert resolve_source_paths(
        video_path=str(base / "Movie.mkv"),
        languages=["zt", "en"],
    ) is None


# --- Deterministic winner when several spellings map to one code ------------


def _resolve_with_glob_order(monkeypatch, video_path, languages, reorder):
    import glob as glob_module
    real_glob = glob_module.glob

    def ordered_glob(pattern, **kwargs):
        return reorder(sorted(real_glob(pattern, **kwargs)))

    monkeypatch.setattr(
        "subtitles.tools.combine.rules.glob.glob", ordered_glob
    )
    return resolve_source_paths(video_path=video_path, languages=languages)


def test_same_code_from_several_spellings_is_deterministic(tmp_path, monkeypatch):
    """.zh-TW, .zht, .cht and .big5 all mean zt at the same priority, so the
    winner used to be whatever the filesystem listed first. Two different
    directory orders must still pick the same file."""
    base = make_video_dir(tmp_path, [
        "Movie.mkv",
        "Movie.en.srt",
        "Movie.zh-TW.srt",
        "Movie.zht.srt",
        "Movie.cht.srt",
        "Movie.big5.srt",
    ])
    video = str(base / "Movie.mkv")

    forward = _resolve_with_glob_order(
        monkeypatch, video, ["zt", "en"], lambda paths: paths
    )
    backward = _resolve_with_glob_order(
        monkeypatch, video, ["zt", "en"], lambda paths: list(reversed(paths))
    )
    assert forward is not None
    assert backward is not None
    assert forward.primary == backward.primary
