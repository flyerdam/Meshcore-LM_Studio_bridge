"""
Web-based services: weather, news, and DuckDuckGo search.
"""

import logging

import requests

from meshcore_bridge.helpers import wmo_code

log = logging.getLogger(__name__)


class WebSearch:

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def weather(self, city: str | None = None) -> str:
        if not city:
            bp = self.cfg.get("bot_prefix", "!bot")
            return f"provide city: {bp} weather London"
        try:
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en"},
                timeout=8,
            )
            results = geo.json().get("results")
            if not results:
                return f"Not found: {city}"
            r    = results[0]
            lat, lon = r["latitude"], r["longitude"]
            name = r.get("name", city)
            cc   = r.get("country_code", "")
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current":  "temperature_2m,precipitation,wind_speed_10m,weathercode",
                    "wind_speed_unit": "ms", "timezone": "auto",
                },
                timeout=8,
            )
            cur  = resp.json()["current"]
            temp = cur.get("temperature_2m", "?")
            prec = cur.get("precipitation", 0)
            wind = cur.get("wind_speed_10m", "?")
            desc = wmo_code(cur.get("weathercode", 0))
            rain = f" rain:{prec}mm" if prec > 0 else ""
            return f"{name}({cc}): {desc} {temp}°C wind:{wind}m/s{rain}"
        except Exception as e:
            log.error("Weather error: %s", e)
            return "Error fetching weather"

    def news(self, query: str | None = None) -> str:
        key = self.cfg.get("news_api_key")
        if not key:
            return "No NewsAPI key. Use --news-key (newsapi.org, free)"
        try:
            if query:
                url, params = "https://newsapi.org/v2/everything", {
                    "q": query, "pageSize": 3, "sortBy": "publishedAt",
                    "language": "en", "apiKey": key,
                }
            else:
                url, params = "https://newsapi.org/v2/top-headlines", {
                    "country": self.cfg.get("news_country", "us"),
                    "pageSize": 3, "apiKey": key,
                }
            arts = requests.get(url, params=params, timeout=10).json().get("articles", [])
            if not arts:
                return "No news"
            return " | ".join(a["title"].split(" - ")[0][:70] for a in arts[:3])
        except Exception as e:
            log.error("NewsAPI error: %s", e)
            return "Error fetching news"

    def search(self, query: str) -> str:
        if not query.strip():
            bp = self.cfg.get("bot_prefix", "!bot")
            return f"provide a query: {bp} search bitcoin"
        try:
            data = requests.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "no_redirect": 1},
                timeout=10,
            ).json()
            abstract = data.get("AbstractText", "").strip()
            if abstract:
                return abstract[:250]
            answer = data.get("Answer", "").strip()
            if answer:
                return answer[:250]
            related = [
                item["Text"][:80]
                for item in data.get("RelatedTopics", [])[:2]
                if isinstance(item, dict) and "Text" in item
            ]
            return " | ".join(related) if related else f"No results for: {query}"
        except Exception as e:
            log.error("DDG error: %s", e)
            return "Search error"
