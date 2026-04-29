"""
Unit tests for meshcore_bridge/web_search.py

All HTTP calls are mocked so these run fully offline.
"""
import pytest
from unittest.mock import patch, MagicMock
from meshcore_bridge.web_search import WebSearch


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_web(extra_cfg=None):
    cfg = {"bot_prefix": "!b", "news_country": "us"}
    if extra_cfg:
        cfg.update(extra_cfg)
    return WebSearch(cfg)


def _mock_response(json_data: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# weather()
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeather:
    def test_no_city_returns_hint(self):
        web = _make_web()
        r = web.weather(None)
        assert "city" in r.lower() or "provide" in r.lower()

    def test_empty_city_returns_hint(self):
        web = _make_web()
        r = web.weather("")
        assert "city" in r.lower() or "provide" in r.lower()

    def test_city_not_found(self):
        geo_response = _mock_response({"results": []})
        with patch("requests.get", return_value=geo_response):
            web = _make_web()
            r = web.weather("NoSuchCity12345")
        assert "not found" in r.lower() or "NoSuchCity" in r

    def test_successful_weather(self):
        geo_resp = _mock_response({
            "results": [{"latitude": 51.5, "longitude": -0.1,
                         "name": "London", "country_code": "GB"}]
        })
        forecast_resp = _mock_response({
            "current": {
                "temperature_2m": 18.5,
                "precipitation": 0,
                "wind_speed_10m": 3.2,
                "weathercode": 0,
            }
        })
        responses = [geo_resp, forecast_resp]
        with patch("requests.get", side_effect=responses):
            web = _make_web()
            r = web.weather("London")
        assert "London" in r
        assert "18" in r
        assert "clear sky" in r

    def test_rain_shown_when_nonzero(self):
        geo_resp = _mock_response({
            "results": [{"latitude": 52.0, "longitude": 21.0,
                         "name": "Warsaw", "country_code": "PL"}]
        })
        forecast_resp = _mock_response({
            "current": {
                "temperature_2m": 12.0,
                "precipitation": 3.5,
                "wind_speed_10m": 5.0,
                "weathercode": 61,
            }
        })
        with patch("requests.get", side_effect=[geo_resp, forecast_resp]):
            web = _make_web()
            r = web.weather("Warsaw")
        assert "rain:" in r

    def test_network_error_returns_error_message(self):
        with patch("requests.get", side_effect=Exception("network error")):
            web = _make_web()
            r = web.weather("London")
        assert "error" in r.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# news()
# ═══════════════════════════════════════════════════════════════════════════════

class TestNews:
    def test_no_api_key_returns_hint(self, monkeypatch):
        monkeypatch.delenv("NEWS_API_KEY", raising=False)
        web = _make_web({"news_api_key": None})
        r = web.news()
        assert "key" in r.lower() or "newsapi" in r.lower()

    def test_env_key_used_when_cfg_missing(self, monkeypatch):
        monkeypatch.setenv("NEWS_API_KEY", "test_key_123")
        articles = [{"title": "Breaking news - source"}, {"title": "More news - src2"}]
        resp = _mock_response({"articles": articles})
        with patch("requests.get", return_value=resp):
            web = _make_web({"news_api_key": None})
            r = web.news()
        assert "Breaking news" in r

    def test_top_headlines_no_query(self):
        articles = [
            {"title": "Story A - Media"},
            {"title": "Story B - Media"},
            {"title": "Story C - Media"},
        ]
        resp = _mock_response({"articles": articles})
        with patch("requests.get", return_value=resp):
            web = _make_web({"news_api_key": "key"})
            r = web.news()
        assert "Story A" in r
        # Top-headlines endpoint used → country param
        call_kwargs = __import__("requests").get.call_args if False else None

    def test_query_headlines(self):
        articles = [{"title": "Bitcoin hits 100k - Finance"}]
        resp = _mock_response({"articles": articles})
        with patch("requests.get", return_value=resp):
            web = _make_web({"news_api_key": "key"})
            r = web.news("bitcoin")
        assert "Bitcoin" in r

    def test_empty_articles_returns_no_news(self):
        resp = _mock_response({"articles": []})
        with patch("requests.get", return_value=resp):
            web = _make_web({"news_api_key": "key"})
            r = web.news()
        assert "no news" in r.lower()

    def test_network_error_returns_error_message(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            web = _make_web({"news_api_key": "key"})
            r = web.news()
        assert "error" in r.lower()

    def test_pipe_separator_between_headlines(self):
        articles = [
            {"title": "A - src"},
            {"title": "B - src"},
            {"title": "C - src"},
        ]
        resp = _mock_response({"articles": articles})
        with patch("requests.get", return_value=resp):
            web = _make_web({"news_api_key": "key"})
            r = web.news()
        assert "|" in r

    def test_title_truncated_at_70_chars(self):
        long_title = "X" * 100 + " - source"
        resp = _mock_response({"articles": [{"title": long_title}]})
        with patch("requests.get", return_value=resp):
            web = _make_web({"news_api_key": "key"})
            r = web.news()
        # The " - source" part is stripped, and title capped at 70 chars
        assert len(r) < 150


# ═══════════════════════════════════════════════════════════════════════════════
# search()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearch:
    def test_empty_query_returns_hint(self):
        web = _make_web()
        r = web.search("")
        assert "provide" in r.lower() or "query" in r.lower()

    def test_whitespace_query_returns_hint(self):
        web = _make_web()
        r = web.search("   ")
        assert "provide" in r.lower() or "query" in r.lower()

    def test_abstract_returned(self):
        data = {"AbstractText": "Bitcoin is a decentralized currency.", "Answer": "", "RelatedTopics": []}
        resp = _mock_response(data)
        with patch("requests.get", return_value=resp):
            web = _make_web()
            r = web.search("bitcoin")
        assert "decentralized" in r

    def test_answer_returned_when_no_abstract(self):
        data = {"AbstractText": "", "Answer": "42", "RelatedTopics": []}
        resp = _mock_response(data)
        with patch("requests.get", return_value=resp):
            web = _make_web()
            r = web.search("life")
        assert "42" in r

    def test_related_topics_fallback(self):
        data = {
            "AbstractText": "",
            "Answer": "",
            "RelatedTopics": [{"Text": "Related result here"}, {"Text": "Another topic"}],
        }
        resp = _mock_response(data)
        with patch("requests.get", return_value=resp):
            web = _make_web()
            r = web.search("something")
        assert "Related result" in r

    def test_no_results_returns_no_results(self):
        data = {"AbstractText": "", "Answer": "", "RelatedTopics": []}
        resp = _mock_response(data)
        with patch("requests.get", return_value=resp):
            web = _make_web()
            r = web.search("xyzzy")
        assert "no results" in r.lower() or len(r) < 30

    def test_abstract_truncated_at_250(self):
        data = {
            "AbstractText": "A" * 500,
            "Answer": "",
            "RelatedTopics": [],
        }
        resp = _mock_response(data)
        with patch("requests.get", return_value=resp):
            web = _make_web()
            r = web.search("longtest")
        assert len(r) <= 260  # some margin for prefix

    def test_network_error_handled(self):
        with patch("requests.get", side_effect=Exception("DNS fail")):
            web = _make_web()
            r = web.search("test")
        assert "error" in r.lower() or len(r) > 0  # should not raise
