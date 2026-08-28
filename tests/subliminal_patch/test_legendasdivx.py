import io
import itertools
import os
import shutil
import stat
import time
import zipfile
from types import SimpleNamespace
from pathlib import Path

import brotli
import rarfile
from guessit import guessit
import pytest
from subliminal.cache import region
from subliminal.exceptions import AuthenticationError, ConfigurationError
from subliminal.video import Movie
from subliminal_patch.core import Episode
from subliminal_patch.score import compute_score
from subliminal_patch.providers import legendasdivx
from subliminal_patch.providers.legendasdivx import LegendasdivxProvider, LegendasdivxSubtitle, extract_release_info
from subzero.language import Language


LOGIN_PAGE_HTML = b"""<!DOCTYPE html>
<html>
<head><title>LegendasDivx - Painel de Controlo do Utilizador - Ligue-se</title></head>
<body>
<form method="post" action="./ucp.php?mode=login">
    <input type="text" name="username" value="" />
    <input type="password" name="password" />
    <input type="hidden" name="sid" value="1890def6632a667396e33b8df3ac94ac" />
    <input type="hidden" name="redirect" value="index.php" />
    <input type="submit" name="login" value="Ligue-se" />
</form>
</body>
</html>"""

SEARCH_PAGE_HTML = b"""<!DOCTYPE html>
<html>
<head><title>LegendasDivx - Downloads</title></head>
<body>
<div class="pager_bar">(1 encontradas)</div>
<div class="sub_box">
    <div class="sub_header">
        <b>The Matrix</b> (1999) - Enviada por: <a href="profile.php?u=100">UploaderName</a>
    </div>
    <table>
        <tr>
            <th>Idioma:</th>
            <td><img src="images/flags/portugal.png" /></td>
            <th>Hits:</th>
            <td>42</td>
            <th>Frame Rate:</th>
            <td>23.976</td>
        </tr>
    </table>
    <td class="td_desc brd_up">Sincronizadas para a release: The.Matrix.1999.1080p.BluRay.x264-SPARKS</td>
    <div class="sub_footer">
        <a class="sub_download" href="?name=Downloads&d_op=getit&lid=12345">Download</a>
    </div>
</div>
</body>
</html>"""


@pytest.fixture(autouse=True)
def configure_cache():
    region.configure("dogpile.cache.memory", replace_existing_backend=True)
    region.delete("legendasdivx_cookies2")


def test_extract_release_info():
    # 1. Strip sync/upload prefixes and keep clean release
    desc1 = "Legendas anteriormente enviadas pelo cristiano170, ressincronizadas por mim para a(s) release(s):\n\n**The.Truman.Show.1998.720p.BluRay.x264-SEPTiC**"
    assert extract_release_info("The Truman Show", 1998, desc1) == "The.Truman.Show.1998.720p.BluRay.x264-SEPTiC"

    # 2. Handle empty or 'Não há descrição disponível'
    desc2 = "Não há descrição disponível"
    assert extract_release_info("The Truman Show", 1998, desc2) == "The Truman Show (1998)"

    # 3. Handle version label prefix
    desc3 = "Sincronizadas para a versão: The.Truman.Show.1998.DVDRip.XviD.AC3-DEViSE"
    assert extract_release_info("The Truman Show", 1998, desc3) == "The.Truman.Show.1998.DVDRip.XviD.AC3-DEViSE"


def test_legendasdivx_config_error():
    with pytest.raises(ConfigurationError):
        LegendasdivxProvider("user", None)
    with pytest.raises(ConfigurationError):
        LegendasdivxProvider(None, "pass")


def test_legendasdivx_login_failure(requests_mock):
    requests_mock.get(
        "https://www.legendasdivx.pt/forum/ucp.php?mode=login",
        content=brotli.compress(LOGIN_PAGE_HTML),
        headers={"Content-Encoding": "br"},
    )
    # Login POST returns user id 1 (anonymous/guest in phpbb)
    requests_mock.post(
        "https://www.legendasdivx.pt/forum/ucp.php?mode=login",
        content=brotli.compress(b"<html><body><div class='error'>Invalid password</div></body></html>"),
        headers={"Content-Encoding": "br"},
    )

    orig_init = LegendasdivxProvider.initialize

    with pytest.raises(AuthenticationError):
        provider = LegendasdivxProvider("wronguser", "wrongpass")
        orig_init(provider)


def test_legendasdivx_login_success(requests_mock):
    requests_mock.get(
        "https://www.legendasdivx.pt/forum/ucp.php?mode=login",
        content=brotli.compress(LOGIN_PAGE_HTML),
        headers={"Content-Encoding": "br"},
    )

    def set_success_cookies(request, context):
        context.headers["Content-Encoding"] = "br"
        return brotli.compress(b"<html><body>Logged in</body></html>")

    requests_mock.post(
        "https://www.legendasdivx.pt/forum/ucp.php?mode=login",
        content=set_success_cookies,
    )

    provider = LegendasdivxProvider("gooduser", "goodpass")
    orig_login = provider.login

    def login_with_cookies():
        provider.session.cookies.set("phpbb3_2z8zs_u", "4242")
        provider.session.cookies.set("phpbb3_2z8zs_sid", "abcdef1234567890")
        provider.session.cookies.set("PHPSESSID", "sess4242")
        provider.session.cookies.set("extra_junk", "discard_me")
        orig_login()

    provider.login = login_with_cookies
    provider.initialize()

    cached_cookies = region.get("legendasdivx_cookies2")
    assert cached_cookies is not None
    assert cached_cookies.get("phpbb3_2z8zs_u") == "4242"
    assert cached_cookies.get("phpbb3_2z8zs_sid") == "abcdef1234567890"
    assert "extra_junk" not in cached_cookies


def test_legendasdivx_query_movie(requests_mock):
    cached_cookies = {
        "phpbb3_2z8zs_u": "4242",
        "phpbb3_2z8zs_sid": "abcdef1234567890",
        "PHPSESSID": "sess4242",
    }
    region.set("legendasdivx_cookies2", cached_cookies)

    requests_mock.get(
        "https://www.legendasdivx.pt/modules.php",
        content=brotli.compress(SEARCH_PAGE_HTML),
        headers={"Content-Encoding": "br"},
    )

    filename = "The.Matrix.1999.1080p.BluRay.x264-SPARKS.mkv"
    movie = Movie.fromguess(filename, guessit(filename))
    movie.imdb_id = "tt0133093"
    movie.fps = 23.976

    provider = LegendasdivxProvider("gooduser", "goodpass", skip_wrong_fps=False)
    provider.initialize()
    subtitles = provider.query(movie, [Language("por")])

    assert len(subtitles) == 1
    sub = subtitles[0]
    assert isinstance(sub, LegendasdivxSubtitle)
    assert sub.hits == 42
    assert sub.title == "The Matrix"
    assert sub.year == 1999
    assert sub.release_info == "The.Matrix.1999.1080p.BluRay.x264-SPARKS"
    matches = sub.get_matches(movie)
    assert "title" in matches
    assert "year" in matches
    # This result is for the right film, but the page carries no imdb id, and a
    # movie imdb_id match expands into title plus year in score.py. Title and
    # year are matched on their own above, so nothing is lost by requiring the
    # id to actually appear. See the two imdb_id tests at the end of this file.
    assert "imdb_id" not in matches
    assert "video_codec" in matches
    assert "resolution" in matches
    assert "release_group" in matches


def _stub_extractors(monkeypatch, tmp_path, behaviour):
    """Replace the real extractors with a stub process under our control.

    behaviour(tool, outdir) stands in for running `tool`: it writes whatever that
    extractor would have produced and returns its exit code, or the string "hang"
    for a process that never exits on its own.
    """
    tried = []
    procs = []
    runs = itertools.count()

    def fake_mkdtemp():
        # a fresh tree per call, since _extract_via_cli really removes its own
        run_dir = tmp_path / f"run{next(runs)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return str(run_dir)

    def outdir_of(cmd):
        if cmd[0] == "unar":
            return Path(cmd[2])
        if cmd[0] == "7z":
            return Path(cmd[3][2:])
        return Path(cmd[4])

    class StubPopen:
        # A hung extractor is only ever stopped by one of the guards. If none of
        # them fires, the poll loop would spin until CLI_EXTRACT_TIMEOUT, so fail
        # fast and say why instead of letting the suite look merely slow.
        max_waits = 20

        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs
            self.killed = False
            self.returncode = None
            self.waits = 0
            tried.append(cmd[0])
            procs.append(self)
            self._result = behaviour(cmd[0], outdir_of(cmd))

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits > self.max_waits:
                raise AssertionError(
                    f"{self.cmd[0]} was polled {self.waits} times without being stopped: "
                    "no time or size guard fired")
            if self._result == "hang" and not self.killed:
                # a real wait() blocks for the poll interval; without that the
                # loop would spin and the deadline would never be reached
                time.sleep(timeout or 0)
                raise legendasdivx.subprocess.TimeoutExpired(self.cmd, timeout)
            if self.returncode is None:
                self.returncode = 0 if self._result == "hang" else self._result
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(legendasdivx.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(legendasdivx.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(legendasdivx.subprocess, "Popen", StubPopen)
    return tried, procs


def _write_subtitle(outdir, name="extracted.srt", body=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n"):
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / name).write_bytes(body)


def test_legendasdivx_cli_extraction_closes_stdin(monkeypatch, tmp_path):
    def behaviour(tool, outdir):
        _write_subtitle(outdir)
        return 0

    _, procs = _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._extract_via_cli(b"archive") is not None
    # an encrypted archive makes extractors prompt; with an inherited terminal
    # that blocks the download worker until the deadline
    assert procs[0].kwargs["stdin"] == legendasdivx.subprocess.DEVNULL


def test_legendasdivx_cli_extraction_kills_an_extractor_that_hangs(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_TIMEOUT", 0.05)

    def behaviour(tool, outdir):
        return "hang"

    tried, procs = _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._extract_via_cli(b"archive") is None
    # every extractor gets its turn, and none of them is left running
    assert tried == ["unar", "7z", "unrar"]
    assert all(proc.killed for proc in procs)


def test_legendasdivx_cli_extraction_moves_on_from_an_extractor_that_hangs(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_TIMEOUT", 0.05)

    def behaviour(tool, outdir):
        if tool == "unar":
            return "hang"
        _write_subtitle(outdir)
        return 0

    tried, _ = _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # a hung unar must not abort the whole fallback: 7z still gets its turn
    assert provider._extract_via_cli(b"archive") is not None
    assert tried == ["unar", "7z"]


def test_legendasdivx_cli_extraction_kills_an_extractor_over_the_size_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_MAX_BYTES", 1024)

    def behaviour(tool, outdir):
        # still running, and already past the budget
        _write_subtitle(outdir, body=b"x" * 4096)
        return "hang"

    _, procs = _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._extract_via_cli(b"archive") is None
    # killed mid-run rather than waited out: the point is to stop the fill, so
    # this has to happen on the first budget poll, not at the timeout
    assert procs[0].killed
    assert procs[0].waits <= 3


def test_legendasdivx_cli_extraction_rejects_an_extractor_over_the_size_budget_at_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_MAX_BYTES", 1024)

    def behaviour(tool, outdir):
        # fast enough to finish between two polls
        _write_subtitle(outdir, body=b"x" * 4096)
        return 0

    _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._extract_via_cli(b"archive") is None


def test_legendasdivx_cli_extraction_kills_an_extractor_over_the_member_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_MAX_MEMBERS", 5)

    def behaviour(tool, outdir):
        for i in range(20):
            _write_subtitle(outdir, name=f"member{i}.srt")
        return "hang"

    _, procs = _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # the same attack aimed at inodes instead of bytes
    assert provider._extract_via_cli(b"archive") is None
    assert procs[0].killed
    assert procs[0].waits <= 3


def test_legendasdivx_cli_extraction_moves_on_after_a_blown_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_MAX_BYTES", 1024)
    good = b"1\n00:00:01,000 --> 00:00:02,000\ngenuine subtitle\n"

    def behaviour(tool, outdir):
        if tool == "unar":
            _write_subtitle(outdir, name="bomb.srt", body=b"x" * 4096)
            return "hang"
        _write_subtitle(outdir, body=good)
        return 0

    tried, _ = _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # a blown budget must behave like every other guard here: give up on that
    # extractor, never raise out of the loop, and let the next one try
    assert provider._extract_via_cli(b"archive") == good
    assert tried == ["unar", "7z"]


def test_legendasdivx_cli_extraction_gives_each_extractor_a_clean_tree(monkeypatch, tmp_path):
    monkeypatch.setattr(legendasdivx, "CLI_EXTRACT_MAX_BYTES", 4096)
    good = b"1\n00:00:01,000 --> 00:00:02,000\ngenuine subtitle\n"
    seen = {}

    def behaviour(tool, outdir):
        outdir.mkdir(parents=True, exist_ok=True)
        seen[tool] = sorted(os.listdir(outdir))
        if tool == "unar":
            _write_subtitle(outdir, name="bomb.srt", body=b"x" * 8192)
            return "hang"
        _write_subtitle(outdir, body=good)
        return 0

    _stub_extractors(monkeypatch, tmp_path, behaviour)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._extract_via_cli(b"archive") == good
    # 7z must not inherit unar's leftovers, or it would trip the budget too and
    # a recoverable archive would look like a bomb
    assert seen["7z"] == []




# ---------------------------------------------------------------------------
# Wrong-episode guards.
#
# Three separate paths used to hand back a subtitle for an episode nobody asked
# for, where the base implementation returned None. That is silent misfiling:
# the file lands in the library, the episode is marked done, and nothing logs an
# error. One test per path, plus a positive control so "always return None" can
# never pass for the fix.
# ---------------------------------------------------------------------------

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SEASON_PACK_RAR = os.path.join(DATA_DIR, "archive_2.rar")


def _episode_subtitle(video):
    data = {
        "link": "https://www.legendasdivx.pt/modules.php?lid=1",
        "hits": 1,
        "exact_match": False,
        "title": "Breaking Bad",
        "year": 2008,
        "description": "Breaking Bad primeira temporada",
        "frame_rate": "0",
        "uploader": "uploader",
        "release_info": "Breaking.Bad.S01",
    }
    return LegendasdivxSubtitle(Language("por"), video, data, skip_wrong_fps=False)


def _zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_legendasdivx_season_pack_returns_none_when_no_episode_matches():
    # archive_2.rar holds S01E01 to S01E07. Listing a rar needs no external
    # extractor, and nothing here reads a member, so this runs anywhere.
    content = open(SEASON_PACK_RAR, "rb").read()
    archive = rarfile.RarFile(io.BytesIO(content))
    assert len(archive.namelist()) == 7

    video = Episode("Breaking.Bad.S01E08.mkv", "Breaking Bad", 1, 8)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # every candidate is filtered out, so there is nothing to return
    assert provider._get_subtitle_from_archive(archive, content, _episode_subtitle(video)) is None


def test_legendasdivx_season_pack_still_returns_the_wanted_episode():
    content = open(SEASON_PACK_RAR, "rb").read()
    archive = rarfile.RarFile(io.BytesIO(content))
    video = Episode("Breaking.Bad.S01E03.mkv", "Breaking Bad", 1, 3)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    reads = []

    def fake_read(name):
        reads.append(name)
        return b"1\n00:00:01,000 --> 00:00:02,000\nepisode three\n"

    archive.read = fake_read

    out = provider._get_subtitle_from_archive(archive, content, _episode_subtitle(video))

    assert out is not None
    assert reads == ["103 - ...And the Bag's in the River.srt"]


def test_legendasdivx_single_subtitle_archive_still_checks_the_episode():
    content = _zip_bytes({"Breaking.Bad.S01E01.srt": "1\n00:00:01,000 --> 00:00:02,000\nepisode one\n"})
    archive = zipfile.ZipFile(io.BytesIO(content))
    video = Episode("Breaking.Bad.S01E05.mkv", "Breaking Bad", 1, 5)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # the shortcut for one-subtitle archives must not skip the episode check
    assert provider._get_subtitle_from_archive(archive, content, _episode_subtitle(video)) is None


def test_legendasdivx_single_subtitle_archive_returns_the_wanted_episode():
    body = "1\n00:00:01,000 --> 00:00:02,000\nepisode five\n"
    content = _zip_bytes({"Breaking.Bad.S01E05.srt": body})
    archive = zipfile.ZipFile(io.BytesIO(content))
    video = Episode("Breaking.Bad.S01E05.mkv", "Breaking Bad", 1, 5)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._get_subtitle_from_archive(archive, content, _episode_subtitle(video)) == body.encode()


def _fake_extractor(monkeypatch, tmp_path, lay_down):
    """A stub extractor that succeeds after writing lay_down(outdir)."""

    def behaviour(tool, outdir):
        outdir.mkdir(parents=True, exist_ok=True)
        lay_down(outdir)
        return 0

    _stub_extractors(monkeypatch, tmp_path, behaviour)


def test_legendasdivx_cli_extraction_never_substitutes_another_member(monkeypatch, tmp_path):
    def lay_down(d):
        (d / "107 - A No-Rough-Stuff-Type Deal.srt").write_bytes(b"wrong episode\n")

    _fake_extractor(monkeypatch, tmp_path, lay_down)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # the caller named the member the scoring loop picked; it is not there, so
    # the answer is nothing, not whatever else happens to be in the archive
    assert provider._extract_via_cli(b"archive", target_name="108 - Wanted.srt") is None


def test_legendasdivx_cli_extraction_skips_symlink_members(monkeypatch, tmp_path):
    secret = tmp_path.parent / "legendasdivx_host_file.txt"
    secret.write_bytes(b"host file content that must never be returned\n")

    def lay_down(d):
        os.symlink(str(secret), str(d / "innocent.srt"))

    _fake_extractor(monkeypatch, tmp_path, lay_down)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    # unar restores symlink members verbatim, absolute targets included
    assert provider._extract_via_cli(b"archive") is None


def test_legendasdivx_cli_extraction_filters_untargeted_members_by_episode(monkeypatch, tmp_path):
    def lay_down(d):
        (d / "107 - A No-Rough-Stuff-Type Deal.srt").write_bytes(b"wrong episode\n")

    _fake_extractor(monkeypatch, tmp_path, lay_down)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    video = Episode("Breaking.Bad.S01E08.mkv", "Breaking Bad", 1, 8)
    assert provider._extract_via_cli(b"archive", video=video) is None

    video = Episode("Breaking.Bad.S01E07.mkv", "Breaking Bad", 1, 7)
    assert provider._extract_via_cli(b"archive", video=video) == b"wrong episode\n"


# ---------------------------------------------------------------------------
# Movie imdb_id claim.
# ---------------------------------------------------------------------------

def _movie_subtitle(video, description, title, year):
    data = {
        "link": "https://www.legendasdivx.pt/modules.php?lid=2",
        "hits": 1,
        "exact_match": False,
        "title": title,
        "year": year,
        "description": description,
        "frame_rate": "0",
        "uploader": "uploader",
        "release_info": description,
    }
    return LegendasdivxSubtitle(Language("por"), video, data, skip_wrong_fps=False)


def test_legendasdivx_movie_imdb_id_needs_evidence_in_the_result():
    video = Movie("The.Matrix.1999.1080p.BluRay.x264-SPARKS.mkv", "The Matrix", year=1999)
    video.imdb_id = "tt0133093"

    # a result for an unrelated film; query() sends imdbid='' for movies, so the
    # backend guarantees nothing here
    subtitle = _movie_subtitle(video, "Legendas para outro filme qualquer", "Totally Different Film", 2015)
    matches = subtitle.get_matches(video)

    assert "imdb_id" not in matches
    # score.py expands a movie imdb_id match into title plus year, 100 of 180
    # points, against a default minimum_score_movie of 126
    assert compute_score(set(matches), subtitle, video)[0] < 126


def test_legendasdivx_movie_imdb_id_claimed_when_the_result_carries_it():
    video = Movie("The.Matrix.1999.1080p.BluRay.x264-SPARKS.mkv", "The Matrix", year=1999)
    video.imdb_id = "tt0133093"

    subtitle = _movie_subtitle(video, "The Matrix (1999) imdb tt0133093 BluRay", "The Matrix", 1999)

    assert "imdb_id" in subtitle.get_matches(video)


@pytest.mark.skipif(shutil.which("unar") is None, reason="needs a real unar to restore symlink members")
def test_legendasdivx_cli_extraction_skips_symlink_members_from_a_real_archive(tmp_path):
    # The fake-extractor test above pins our guard. This one pins the premise:
    # unar really does restore a symlink member, absolute target included.
    secret = tmp_path / "host_file.txt"
    secret.write_bytes(b"host file content that must never be returned\n")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("innocent.srt")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, str(secret))
        zf.writestr("real.srt", "1\n00:00:01,000 --> 00:00:02,000\ngenuine subtitle\n")
    payload = buf.getvalue()

    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)
    out = provider._extract_via_cli(payload)

    assert out is not None
    assert b"host file content" not in out
    assert b"genuine subtitle" in out


def test_legendasdivx_anime_absolute_numbering_is_accepted():
    """Anime archives are numbered absolutely while Sonarr stores the video as
    season-relative, with the absolute number on video.absolute_episode. The
    episode guard has to honour both or every anime download is rejected."""
    body = "1\n00:00:01,000 --> 00:00:02,000\nepisode three ten\n"
    content = _zip_bytes({"[HorribleSubs] One Piece - 310 [1080p].srt": body})
    archive = zipfile.ZipFile(io.BytesIO(content))
    video = Episode("One.Piece.S10E14.mkv", "One Piece", 10, 14)
    video.absolute_episode = 310
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._get_subtitle_from_archive(archive, content, _episode_subtitle(video)) == body.encode()


def test_legendasdivx_absolute_numbering_does_not_accept_any_episode():
    content = _zip_bytes({"[HorribleSubs] One Piece - 311 [1080p].srt": "1\n00:00:01,000 --> 00:00:02,000\nwrong\n"})
    archive = zipfile.ZipFile(io.BytesIO(content))
    video = Episode("One.Piece.S10E14.mkv", "One Piece", 10, 14)
    video.absolute_episode = 310
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._get_subtitle_from_archive(archive, content, _episode_subtitle(video)) is None


def test_legendasdivx_extraction_budget_counts_directories(tmp_path):
    """An archive of nothing but empty directories writes no bytes and no
    files, so a files-only count leaves the budget at zero while the extractor
    eats one inode per member until the timeout."""
    from subliminal_patch.providers.legendasdivx import CLI_EXTRACT_MAX_MEMBERS

    outdir = tmp_path / "out"
    for i in range(CLI_EXTRACT_MAX_MEMBERS + 5):
        (outdir / f"d{i}").mkdir(parents=True)

    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._over_extraction_budget(str(outdir), "unzip") is True


def _entry(name, path, is_dir=False, size=0):
    """Enough of os.DirEntry for the scanner: it reads name, is_dir and stat."""
    return SimpleNamespace(
        name=name,
        path=path,
        is_dir=lambda follow_symlinks=True: is_dir,
        stat=lambda follow_symlinks=True: SimpleNamespace(st_size=size),
    )


class _CountingScandir:
    """A directory that never ends, counting how far the scanner got into it."""

    def __init__(self, limit):
        self.seen = 0
        self._limit = limit

    def __call__(self, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        while True:
            self.seen += 1
            if self.seen > self._limit:
                raise AssertionError(
                    f"the scan read {self.seen} entries past its own cap"
                )
            yield _entry(f"f{self.seen}", f"/root/f{self.seen}")


def test_legendasdivx_extraction_budget_stops_reading_once_it_is_over(monkeypatch, tmp_path):
    """The count exists to cut a running extractor short, so it has to stop
    inside the directory that blew it.

    os.walk lists every name in a directory before it yields anything, so an
    archive that writes millions of entries into one directory would be
    enumerated in full before any budget could be consulted, with the extractor
    still running and the deadline unreachable.
    """
    from subliminal_patch.providers import legendasdivx as mod

    scandir = _CountingScandir(mod.CLI_EXTRACT_MAX_MEMBERS + 10)
    monkeypatch.setattr(mod.os, "scandir", scandir)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    assert provider._over_extraction_budget(str(tmp_path), "unzip") is True
    assert scandir.seen <= mod.CLI_EXTRACT_MAX_MEMBERS + 1


def test_legendasdivx_cli_extraction_reads_the_episode_from_the_directory(monkeypatch, tmp_path):
    """Some packs put the episode in the directory and call every file the
    same thing. Matching the basename alone accepts both, so a request for E02
    silently gets E01."""
    def lay_down(d):
        for episode in ("S01E01", "S01E02"):
            folder = d / f"Show.{episode}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "subtitle.srt").write_bytes(f"{episode} subtitle\n".encode())

    _fake_extractor(monkeypatch, tmp_path, lay_down)

    # os.walk hands back filesystem order, so pin it: E01 first is the order
    # that makes a basename-only match hand over the wrong episode.
    from subliminal_patch.providers import legendasdivx as mod
    real_walk = os.walk

    def ordered_walk(top):
        for root, dirs, files in real_walk(top):
            # In place: os.walk recurses through this very list, so replacing it
            # with a sorted copy would leave the traversal order untouched.
            dirs.sort()
            files.sort()
            yield root, dirs, files

    monkeypatch.setattr(mod.os, "walk", ordered_walk)

    video = Episode("Show.S01E02.mkv", "Show", 1, 2)
    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)

    out = provider._extract_via_cli(b"archive-bytes", None, video)

    assert out == b"S01E02 subtitle\n"
