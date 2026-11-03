"""Scrape every Bellhaven community from the public website.

The directory (/communities) is paginated, and the homepage can announce
communities not yet listed there (e.g. a new acquisition), so we crawl both:
every /communities/<slug> link found on the homepage, About page, and every
directory page.
"""
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE = "https://analyst-assessment-production.up.railway.app"


def _get(session, path):
    r = session.get(urljoin(BASE, path), timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def _community_links(soup):
    return {a["href"] for a in soup.select('a[href^="/communities/"]')}


def discover_slugs(session):
    links = set()
    for path in ("/", "/about"):
        links |= _community_links(_get(session, path))
    page, seen_pages = "/communities", set()
    while page and page not in seen_pages:
        seen_pages.add(page)
        soup = _get(session, page)
        links |= _community_links(soup)
        nxt = soup.select_one(".pager a[href*='page=']:-soup-contains('Next')")
        page = nxt["href"] if nxt else None
    return sorted(links)


def parse_detail(soup, url):
    fields = {}
    for dt in soup.select("dl.detail dt"):
        fields[dt.get_text(strip=True).lower()] = dt.find_next_sibling("dd")
    addr_lines = [s.strip() for s in fields["address"].stripped_strings]
    street, cityline = addr_lines[0], addr_lines[-1]
    m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", cityline)
    city, state, zip_ = (m.group(1), m.group(2), m.group(3)) if m else (cityline, "", "")
    txt = lambda k: fields[k].get_text(" ", strip=True) if k in fields else ""
    return {
        "slug": url.rstrip("/").rsplit("/", 1)[-1],
        "url": urljoin(BASE, url),
        "name": soup.select_one(".wrap h1").get_text(strip=True),
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_,
        "care_offerings": [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")]
        if "care offerings" in fields else [],
        "administrator": txt("administrator"),
        "phone": txt("phone"),
    }


def scrape():
    s = requests.Session()
    return [parse_detail(_get(s, link), link) for link in discover_slugs(s)]


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), indent=1))
