"""Curated table of popular RSS/Atom feeds, grouped by category, for the
Settings -> News Feeds "Browse popular feeds" picker (ROADMAP: RSS
subscription list). Every URL here was checked by hand (HTTP 200 + an
``<rss``/``<feed``/``<?xml`` sniff on the response body) before being added --
see the implementation session's shell history if a URL ever needs
re-verifying. This is a static, hand-maintained list, not fetched from
anywhere; users can always add any other feed via the plain URL box next to
this picker in Settings.

"Local News" is necessarily a compromise: there's no single feed that's
"local" for every user, so that category lists well-known *national* (US)
outlets rather than pretending to know the user's location -- true local
coverage still has to be added by hand via the custom-URL box.
"""

POPULAR_FEEDS: dict[str, list[dict[str, str]]] = {
    "General News": [
        {"title": "BBC News", "url": "http://feeds.bbci.co.uk/news/rss.xml"},
        {"title": "NPR News", "url": "https://feeds.npr.org/1001/rss.xml"},
        {"title": "The Guardian", "url": "https://www.theguardian.com/international/rss"},
        {"title": "Google News: Top Stories",
         "url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"},
    ],
    "World News": [
        {"title": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
        {"title": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"title": "The Guardian World", "url": "https://www.theguardian.com/world/rss"},
    ],
    "Local News (US)": [
        {"title": "NPR National", "url": "https://feeds.npr.org/1003/rss.xml"},
        {"title": "CBS News US", "url": "https://www.cbsnews.com/latest/rss/us"},
        {"title": "ABC News US", "url": "https://abcnews.go.com/abcnews/usheadlines"},
    ],
    "Tech News": [
        {"title": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
        {"title": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
        {"title": "Hacker News (Y Combinator)", "url": "https://news.ycombinator.com/rss"},
        {"title": "TechCrunch", "url": "https://techcrunch.com/feed/"},
        {"title": "Wired", "url": "https://www.wired.com/feed/rss"},
    ],
    "Cybersecurity": [
        {"title": "Krebs on Security", "url": "https://krebsonsecurity.com/feed/"},
        {"title": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
        {"title": "Schneier on Security", "url": "https://www.schneier.com/feed/atom/"},
        {"title": "The Hacker News", "url": "https://feeds.feedburner.com/TheHackersNews"},
        {"title": "Dark Reading", "url": "https://www.darkreading.com/rss.xml"},
    ],
    "Amateur Radio": [
        {"title": "ARRL News", "url": "http://www.arrl.org/news/rss"},
        {"title": "AmateurRadio.com", "url": "https://www.amateurradio.com/feed/"},
        {"title": "RSGB News", "url": "https://rsgb.org/main/feed/"},
    ],
    "Electronics": [
        {"title": "Hackaday", "url": "https://hackaday.com/feed/"},
        {"title": "Adafruit Blog", "url": "https://blog.adafruit.com/feed/"},
        {"title": "All About Circuits", "url": "https://www.allaboutcircuits.com/rss/news/"},
        {"title": "IEEE Spectrum", "url": "https://spectrum.ieee.org/feeds/feed.rss"},
    ],
    "Sports": [
        {"title": "ESPN", "url": "https://www.espn.com/espn/rss/news"},
        {"title": "BBC Sport", "url": "http://feeds.bbci.co.uk/sport/rss.xml"},
        {"title": "CBS Sports", "url": "https://www.cbssports.com/rss/headlines/"},
        {"title": "Sky Sports", "url": "https://www.skysports.com/rss/12040"},
    ],
}
