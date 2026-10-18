"""The v2 dashboard page (SPEC_V2 §1-§3, §6, §11): served, self-contained, safe, and free of
technical words on the default screens. Static checks only (no browser, no network)."""
from __future__ import annotations

import html
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agency.dashboard.app import STATIC_DIR, TEMPLATES_DIR, create_app

HEADERS = {"X-Agency-Request": "1"}
PC = ("127.0.0.1", 50000)

INDEX = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
STYLE = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

# SPEC_V2 "Banned words on default (non-advanced) screens" (+ the Hebrew spelling of token).
BANNED = re.compile(r"dry.?run|\bapi\b|\btoken|\bsecret|oauth|webhook|\bjson\b|\bconfig|\byaml\b|"
                    r"\benv\b|pipeline|טוקן|#\d", re.IGNORECASE)
# Links the owner opens himself from the connect wizards (never loaded by the page).
ALLOWED_URLS = {
    "http://www.w3.org/2000/svg",  # SVG namespace, not a request
    "https://zernio.com",
    "https://developers.facebook.com/tools/explorer",
    "https://developers.facebook.com/tools/debug/accesstoken",
    "https://t.me/BotFather",
    "https://t.me/",
    "https://developer.x.com",
    "https://www.reddit.com/prefs/apps",
    "http://localhost:8080",       # text the owner types into Reddit's form
    "https://discord.com/",        # placeholder text
}


@pytest.fixture
def client(ctx, no_network):
    app = create_app(ctx)
    app.state.inline_jobs = True
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC, headers=HEADERS) as c:
        yield c


class _Visible(HTMLParser):
    """Text a person sees or hears: text nodes + aria-label / placeholder / title / alt."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        for name, value in attrs:
            if name in ("aria-label", "placeholder", "title", "alt") and value:
                self.parts.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _default_screen_js() -> str:
    """app.js without the connect-wizard copy (which quotes the platforms' own button labels)
    and without the advanced page (technical details are allowed there)."""
    out = re.sub(r"/\* WIZARD-COPY:BEGIN.*?/\* WIZARD-COPY:END \*/", "", APP_JS, flags=re.S)
    out = re.sub(r"/\* ADVANCED:BEGIN \*/.*?/\* ADVANCED:END \*/", "", out, flags=re.S)
    assert len(out) < len(APP_JS) * 0.85, "section markers missing"
    return out


def _hebrew_literals(source: str) -> list[str]:
    strings = re.findall(r'"((?:[^"\\\n]|\\.)*)"', source)
    return [s for s in strings if re.search(r"[֐-׿]", s)]


# --------------------------------------------------------------------------- served
def test_page_is_served_hebrew_rtl_with_all_screens(client):
    res = client.get("/")
    assert res.status_code == 200
    page = res.text
    assert '<html lang="he" dir="rtl">' in page
    assert 'name="viewport"' in page and "Imagination" in page
    for section in ("page-home", "page-connections", "page-settings", "page-advanced"):
        assert f'id="{section}"' in page
    for route in ("#/connections", "#/settings", "#/advanced"):
        assert f'href="{route}"' in page
    assert 'src="/static/app.js"' in page and 'href="/static/style.css"' in page
    js = client.get("/static/app.js")
    css = client.get("/static/style.css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert "default-src 'self'" in res.headers["content-security-policy"]


def test_template_values_are_escaped(ctx, no_network):
    from agency.settings_store import update_config

    update_config(ctx.settings, {"channel": {"name": '"><script>alert(1)</script>'}})
    app = create_app(ctx)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC, headers=HEADERS) as c:
        page = c.get("/").text
    assert "<script>alert(1)" not in page
    assert html.escape('"><script>', quote=True) in page or "&#34;&gt;&lt;script&gt;" in page


# --------------------------------------------------------------------------- self-contained
def test_no_external_assets_or_cdn():
    for text in (INDEX, APP_JS, STYLE):
        low = text.lower()
        assert "cdn" not in low and "googleapis" not in low and "@import" not in low
        assert not re.search(r"url\(\s*['\"]?https?:", low)
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=\"https?:", INDEX, re.I)
    # the only absolute URL in the page is the SVG namespace inside the data: favicon
    assert set(re.findall(r"https?://[^\s'\"<>%)]+", INDEX)) <= {"http://www.w3.org/2000/svg"}
    for url in re.findall(r"https?://[^\s\"'<>)]+", APP_JS):
        assert url.rstrip("/…") in {u.rstrip("/") for u in ALLOWED_URLS} or url in ALLOWED_URLS, url


def test_page_works_under_the_default_csp():
    # CSP is script-src 'self' / style-src 'self': no inline script, style or handlers
    assert not re.search(r"<script(?![^>]*\bsrc=)", INDEX)
    assert "<style" not in INDEX.lower()
    assert not re.search(r"\sstyle=\"", INDEX)
    assert not re.search(r"\son[a-z]+=\"", INDEX, re.I)
    assert "setAttribute(\"style\"" not in APP_JS


# --------------------------------------------------------------------------- safe DOM
def test_script_never_writes_html_from_data():
    for pattern in (r"\.innerHTML\s*=", r"\.outerHTML\s*=", r"insertAdjacentHTML", r"document\.write",
                    r"\beval\s*\(", r"new Function", r"\.html\s*\("):
        assert not re.search(pattern, APP_JS), pattern
    assert "textContent" in APP_JS and "createElementNS" in APP_JS
    # links are http(s) only, thumbnails only from our own endpoint, QR only as an <img>
    assert 'u.protocol === "http:" || u.protocol === "https:"' in APP_JS
    assert r"/^\/api\/videos\/\d+\/thumbnail$/" in APP_JS
    assert '"data:image/svg+xml;charset=utf-8," + encodeURIComponent(' in APP_JS
    assert 'rel: "noopener noreferrer"' in APP_JS


def test_every_request_carries_the_csrf_header():
    assert 'const HEADERS = { "X-Agency-Request": "1" };' in APP_JS
    assert 'headers: Object.assign({}, HEADERS)' in APP_JS
    assert 'xhr.setRequestHeader("X-Agency-Request", "1")' in APP_JS
    assert 'credentials: "same-origin"' in APP_JS


def test_secret_fields_are_masked_and_never_echoed():
    assert 'type: isSecret ? "password" : "text"' in APP_JS
    assert 'autocomplete: "off"' in APP_JS and 'spellcheck: "false"' in APP_JS
    assert "wipe(ctx.data.fields)" in APP_JS  # typed keys are cleared after saving


# --------------------------------------------------------------------------- API contract
def _route_accepts(literal: str, route: str) -> bool:
    """``literal`` is a full path of ``route`` ({param} = one segment) or the fixed start of
    one (the page appends ids / platform keys to it)."""
    if re.fullmatch(re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(route)), literal):
        return True
    return route.startswith(literal)


def test_every_api_path_the_page_calls_exists(client):
    routes = [getattr(r, "path", "") for r in client.app.routes]
    used = {p.split("?")[0] for p in re.findall(r'"(/api/[a-z0-9_/\-?=]*)', APP_JS)}
    assert {"/api/home", "/api/connections", "/api/settings", "/api/remote", "/api/telegram/pair",
            "/api/inbox/prepare", "/api/connect/import/scan", "/api/upload", "/api/chat"} <= used
    for path in used:
        assert any(_route_accepts(path, r) for r in routes), path


def test_home_polls_every_three_seconds_and_slows_down_when_hidden():
    assert "const POLL_MS = 3000;" in APP_JS
    # hidden tab: still polled (slower), or the browser notification could never fire
    assert "const HIDDEN_EVERY = 10;" in APP_JS
    assert re.search(r"if \(document\.hidden\) \{\s*//[^\n]*\n(\s*//[^\n]*\n)*\s*if \(tick % "
                     r"HIDDEN_EVERY === 0\) \{ refreshHome\(\); refreshPosts\(\); \}", APP_JS)
    assert "!canNotify() || Notification.permission !== \"granted\" || !document.hidden" in APP_JS
    assert 'api("/api/home")' in APP_JS
    assert "visibilitychange" in APP_JS


def test_live_switch_asks_before_going_live():
    assert "מעכשיו הקליפים יעלו באמת ל-" in APP_JS
    assert "{ json: { live: on, confirm: on } }" in APP_JS


def test_advanced_mode_is_remembered_locally():
    assert "localStorage" in APP_JS and "imagination.advanced" in APP_JS
    assert 'next.page === "advanced" && !advancedOn()' in APP_JS


# --------------------------------------------------------------------------- owner wording
def test_default_screens_have_no_technical_words():
    parser = _Visible()
    parser.feed(INDEX)
    visible = parser.parts
    assert "זרוק קליפ" in visible and "מחכה לך" in " ".join(visible)
    for text in visible:
        assert not BANNED.search(text), text
    for text in _hebrew_literals(_default_screen_js()):
        assert not BANNED.search(text), text
    # "מפתח" (key) is a wizard-only word (SPEC_V2 banned-words list)
    for text in visible + _hebrew_literals(_default_screen_js()):
        if "מפתח" in text:
            assert text.startswith("להדביק"), text  # only the import flow's next-step labels


def test_spec_wording_is_present():
    for words in ("יאללה, תעלה", "לא עכשיו", "שנה משהו", "איך הולך", "מצב אימון", "שידור חי",
                  "העלאה אמיתית", "ייבוא חיבורים קיימים", "מצב מתקדם", "קליפים קודמים",
                  "תכין לפרסום", "התנתק מכל המכשירים", "מה המצב?", "מה עלה היום?",
                  "תן לי רעיון לקליפ הבא", "כמה הוצאנו החודש?", "ברגע שיעלה הקליפ הראשון תראה פה מספרים"):
        assert words in INDEX or words in APP_JS, words


# --------------------------------------------------------------------------- accessibility
def test_accessibility_basics():
    assert ":focus-visible" in STYLE and "prefers-reduced-motion" in STYLE
    assert 'class="skip"' in INDEX and 'id="main"' in INDEX
    assert 'role="switch"' in INDEX and 'aria-live="polite"' in INDEX
    for label in ('aria-label="חיבורים"', 'aria-label="הגדרות"', 'aria-label="שלח"'):
        assert label in INDEX
    # icon-only buttons in the template all carry a name
    for tag in re.findall(r"<button[^>]*>\s*</button>", INDEX):
        assert "aria-label=" in tag, tag


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_script_parses():
    out = subprocess.run(["node", "--check", str(STATIC_DIR / "app.js")], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
