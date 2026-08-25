import brotli
from guessit import guessit
import pytest
from subliminal.cache import region
from subliminal.exceptions import AuthenticationError, ConfigurationError
from subliminal.video import Movie
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
    assert "imdb_id" in matches
    assert "video_codec" in matches
    assert "resolution" in matches
    assert "release_group" in matches


def test_legendasdivx_cli_extraction_uses_bounded_subprocess_timeout(monkeypatch, tmp_path):
    calls = []

    class Proc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        # the extractor "wrote" the subtitle the caller asked for
        (tmp_path / "extracted.srt").write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        return Proc()

    monkeypatch.setattr(legendasdivx.tempfile, "mkdtemp", lambda: str(tmp_path))
    monkeypatch.setattr(legendasdivx.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(legendasdivx.shutil, "rmtree", lambda *args, **kwargs: None)
    monkeypatch.setattr(legendasdivx.subprocess, "run", fake_run)

    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)
    assert provider._extract_via_cli(b"not-a-real-archive") is not None

    assert len(calls) == 1
    assert calls[0][1]["timeout"] == legendasdivx.CLI_EXTRACT_TIMEOUT


def test_legendasdivx_cli_extraction_skips_extractor_that_times_out(monkeypatch, tmp_path):
    tried = []

    class Proc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        tried.append(cmd[0])
        if cmd[0] == "unar":
            raise legendasdivx.subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        (tmp_path / "extracted.srt").write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        return Proc()

    monkeypatch.setattr(legendasdivx.tempfile, "mkdtemp", lambda: str(tmp_path))
    monkeypatch.setattr(legendasdivx.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(legendasdivx.shutil, "rmtree", lambda *args, **kwargs: None)
    monkeypatch.setattr(legendasdivx.subprocess, "run", fake_run)

    provider = LegendasdivxProvider.__new__(LegendasdivxProvider)
    # a hung unar must not abort the whole fallback: 7z still gets its turn
    assert provider._extract_via_cli(b"not-a-real-archive") is not None
    assert tried == ["unar", "7z"]
