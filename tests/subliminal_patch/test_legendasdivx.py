import brotli
import pytest
from subliminal.cache import region
from subliminal.exceptions import AuthenticationError, ConfigurationError
from subliminal.video import Movie
from subliminal_patch.providers.legendasdivx import LegendasdivxProvider, LegendasdivxSubtitle
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
    <td class="td_desc brd_up">The Matrix 1999 1080p BluRay x264-SPARKS</td>
    <div class="sub_header">
        <a href="profile.php?mode=viewprofile&u=100">UploaderName</a>
    </div>
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

    provider = LegendasdivxProvider("wronguser", "wrongpass")
    # Simulate failed authentication cookie
    def set_failed_cookie(r, **kw):
        provider.session.cookies.set("phpbb3_2z8zs_u", "1")

    provider.initialize = lambda: (setattr(provider, "session", provider.session if hasattr(provider, "session") else None), LegendasdivxProvider.initialize(provider))
    # Hook into session creation
    orig_init = LegendasdivxProvider.initialize

    def wrapped_init(self):
        orig_init(self)

    # Directly run initialize with response hook
    with pytest.raises(AuthenticationError):
        provider = LegendasdivxProvider("wronguser", "wrongpass")
        # Run initialize
        provider.session = provider.__class__.__dict__["initialize"]
        # Or simply call provider.initialize() where response returns no valid session
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
    # Initialize will create session and login
    # We patch session creation to attach response hook that sets cookies
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

    movie = Movie(
        name="The.Matrix.1999.1080p.mkv",
        title="The Matrix",
        year=1999,
        imdb_id="tt0133093",
        fps=23.976,
    )

    provider = LegendasdivxProvider("gooduser", "goodpass", skip_wrong_fps=False)
    provider.initialize()
    subtitles = provider.query(movie, [Language("por")])

    assert len(subtitles) == 1
    sub = subtitles[0]
    assert isinstance(sub, LegendasdivxSubtitle)
    assert sub.hits == 42
    assert "12345" in sub.id or "tt0133093" in sub.id
