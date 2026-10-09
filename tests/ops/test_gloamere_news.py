"""Release feed selection must exclude noisy prereleases and stale updates."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "ops/gloamere/news_digest.py"
SPEC = importlib.util.spec_from_file_location("gloamere_news_digest", SOURCE)
news = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(news)
NOW = datetime(2026, 10, 10, 1, 30, tzinfo=timezone.utc)


def feed(title, *, date="2026-10-09T12:00:00Z", url="https://github.com/example/project/releases/tag/v1"):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>{title}</title><published>{date}</published><updated>2026-10-10T01:00:00Z</updated>
    <link href="{url}"/><content type="html">&lt;p&gt;Fix &amp;amp; improve.&lt;/p&gt;</content>
    </entry></feed>'''.encode()


def test_formal_release_keeps_source_link_and_readable_excerpt():
    items = news.parse_feed(feed("Hermes Agent v0.21.6"), "Hermes", NOW)
    assert len(items) == 1
    assert items[0]["title"] == "Hermes Agent v0.21.6"
    assert items[0]["excerpt"] == "Fix & improve."
    assert items[0]["url"].endswith("/releases/tag/v1")


def test_release_candidates_and_abandoned_canaries_are_excluded():
    titles = ["rc.5-v0.21.7", "abandoned-rc.4-v0.21.7", "rc.4-v0.21.6",
              "0.163.0-alpha.4", "v0.21.6+canary.20261009T070410Z", "1.0.0-rc1", "v2.0-beta"]
    for title in titles:
        assert news.parse_feed(feed(title), "Hermes", NOW) == []


def test_editing_an_old_release_does_not_make_it_new():
    assert news.parse_feed(feed("v1", date="2026-09-01T00:00:00Z"), "Codex", NOW) == []
    assert news.parse_feed(feed("v1", date="2026-11-01T00:00:00Z"), "Codex", NOW) == []


def test_unexpected_links_and_unbounded_excerpts_are_rejected_or_clipped():
    assert news.parse_feed(feed("v1", url="http://example.com/release"), "Codex", NOW) == []
    assert len(news.plain_text("<div>" + "a" * 2000 + "</div>")) == 800
