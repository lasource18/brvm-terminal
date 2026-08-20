"""Tests for the sources._dedupe helper."""

from __future__ import annotations

from brvm.sources._dedupe import news_hash


def test_hash_is_deterministic():
    assert news_hash("https://x/a", "Title") == news_hash("https://x/a", "Title")


def test_hash_normalizes_case_and_trailing_slash():
    a = news_hash("https://Example.com/A/", "Foo Bar")
    b = news_hash("https://example.com/a", "Foo Bar")
    assert a == b


def test_hash_normalizes_whitespace_in_title():
    a = news_hash("https://x/a", "  Foo   bar  ")
    b = news_hash("https://x/a", "foo bar")
    assert a == b


def test_hash_differs_when_urls_diverge():
    a = news_hash("https://x/a", "Foo")
    b = news_hash("https://x/b", "Foo")
    assert a != b


def test_hash_differs_when_titles_diverge():
    a = news_hash("https://x/a", "Foo")
    b = news_hash("https://x/a", "Bar")
    assert a != b


def test_hash_ignores_query_and_fragment():
    a = news_hash("https://x/a?utm=1", "Foo")
    b = news_hash("https://x/a#top", "Foo")
    c = news_hash("https://x/a", "Foo")
    assert a == b == c
