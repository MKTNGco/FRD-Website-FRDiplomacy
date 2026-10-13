#!/usr/bin/env python3
"""Read-only catalog of https://frdiplomacy.org (Squarespace 7.1).

Does not authenticate and does not modify the live site.
One request per second. Steps are resumable and write under the repo root.

  python3 scripts/archive_site.py discover
  python3 scripts/archive_site.py capture
  python3 scripts/archive_site.py render
  python3 scripts/archive_site.py normalize
  python3 scripts/archive_site.py assets
  python3 scripts/archive_site.py inventory
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parents[1]
RAW_JSON = ROOT / "raw" / "json"
RAW_HTML = ROOT / "raw" / "html"
RAW_MD = ROOT / "raw" / "markdown"
ASSETS = ROOT / "assets"
CONTENT_PAGES = ROOT / "content" / "pages"
CONTENT_POSTS = ROOT / "content" / "posts"
INVENTORY = ROOT / "inventory"
DISCOVERY_PATH = INVENTORY / "discovery.json"
FAILURES_PATH = INVENTORY / "failures.jsonl"

ORIGIN = "https://www.frdiplomacy.org"
SITEMAP_URL = "https://frdiplomacy.org/sitemap.xml"
HOSTS = {"frdiplomacy.org", "www.frdiplomacy.org"}
UA = "FRD-Archive/1.0 (read-only inventory of frdiplomacy.org)"
RATE_SECONDS = 1.0
TIMEOUT = 45
SITE_TZ = ZoneInfo("America/Los_Angeles")

ASSET_EXT = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mp3", ".zip", ".doc", ".docx", ".ppt", ".pptx", ".csv",
    ".tif", ".tiff", ".avif",
}
SKIP_ASSET_HOSTS = {
    "definitions.sqspcdn.com",
    "static1.squarespace.com",
    "assets.squarespace.com",
}
CONTENT_ASSET_HOSTS = {"images.squarespace-cdn.com"}

# Public paths suggested by a prior review and Squarespace defaults.
# These are fetched so omissions in the sitemap are visible. Not a wordlist.
PROBE_PATHS = [
    "/",
    "/home",
    "/home/",
    "/frd-method",
    "/method",
    "/the-frd-method",
    "/cart",
    "/checkout",
    "/commerce",
    "/store",
    "/shop",
    "/products",
    "/search",
    "/donate",
    "/faq",
    "/faqs",
    "/privacy",
    "/privacy-policy",
    "/terms",
    "/terms-of-service",
    "/account",
    "/login",
    "/news",
    "/journal",
    "/events",
    "/partners",
    "/resources",
    "/s/FAQs.pdf",
]

_last_request = 0.0
_CACHE: dict[str, dict] = {}


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(step: str, url: str, error: str) -> None:
    INVENTORY.mkdir(parents=True, exist_ok=True)
    rec = {
        "step": step,
        "url": url,
        "error": error,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    with FAILURES_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"FAIL [{step}] {url} :: {error}")


def load_discovery() -> dict:
    if DISCOVERY_PATH.exists():
        return json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "origin": ORIGIN,
        "sitemap_url": SITEMAP_URL,
        "urls": {},
        "header_nav": [],
        "footer_links": [],
        "blog_items": [],
        "notes": [],
    }


def save_discovery(data: dict) -> None:
    INVENTORY.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    DISCOVERY_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _throttle() -> None:
    global _last_request
    wait = RATE_SECONDS - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def http_fetch(url: str) -> dict:
    """Single request via curl. Does not follow redirects."""
    cached = _CACHE.get(url)
    if cached is not None:
        return cached
    _throttle()
    body_file = ROOT / ".fetch-body.tmp"
    try:
        proc = subprocess.run(
            [
                "curl", "-sS",
                "-D", "-",
                "-o", str(body_file),
                "-A", UA,
                "--max-time", str(TIMEOUT),
                "-w", "\n__STATUS__%{http_code}",
                url,
            ],
            capture_output=True,
        )
    except Exception as exc:
        result = {
            "url": url,
            "status": 0,
            "headers": {},
            "body": b"",
            "location": None,
            "error": f"{type(exc).__name__}: {exc}",
            "content_type": "",
        }
        _CACHE[url] = result
        return result
    raw_out = proc.stdout.decode("utf-8", errors="replace")
    status = 0
    if "__STATUS__" in raw_out:
        raw_out, status_s = raw_out.rsplit("__STATUS__", 1)
        status = int(status_s.strip() or "0")
    headers: dict[str, str] = {}
    for line in raw_out.splitlines():
        if line.lower().startswith("http/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    body = body_file.read_bytes() if body_file.exists() else b""
    if proc.returncode != 0 and status == 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip() or f"curl exit {proc.returncode}"
        result = {
            "url": url,
            "status": 0,
            "headers": headers,
            "body": body,
            "location": None,
            "error": err,
            "content_type": headers.get("content-type", ""),
        }
        _CACHE[url] = result
        return result
    location = headers.get("location")
    if location:
        location = urllib.parse.urljoin(url, location.strip())
    result = {
        "url": url,
        "status": status,
        "headers": headers,
        "body": body,
        "location": location,
        "error": None,
        "content_type": headers.get("content-type", ""),
    }
    _CACHE[url] = result
    return result


def follow(url: str, limit: int = 10) -> dict:
    """Follow redirects and return the chain plus the final response."""
    chain = []
    current = url
    seen = set()
    final = None
    for _ in range(limit):
        if current in seen:
            chain.append({"url": current, "status": 0, "location": None, "error": "redirect loop"})
            break
        seen.add(current)
        got = http_fetch(current)
        hop = {
            "url": current,
            "status": got["status"],
            "location": got["location"],
            "error": got["error"],
            "content_type": got["content_type"],
        }
        chain.append(hop)
        final = got
        if got["status"] in {301, 302, 303, 307, 308} and got["location"]:
            current = got["location"]
            continue
        break
    return {"chain": chain, "final": final, "requested": url}


def norm_url(url: str, base: str | None = None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return None
    if base:
        url = urllib.parse.urljoin(base, url)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    # Drop fragment. Keep query for now; callers decide.
    path = parts.path or "/"
    # Collapse accidental duplicate slashes but keep a single trailing slash.
    path = re.sub(r"/{2,}", "/", path)
    query = parts.query
    return urllib.parse.urlunsplit((parts.scheme.lower(), host, path, query, ""))


def is_internal(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in HOSTS


def strip_query(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))


def path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path or "/"


def is_asset_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    ext = Path(urllib.parse.unquote(parts.path)).suffix.lower()
    if host in CONTENT_ASSET_HOSTS:
        return True
    if is_internal(url) and ext in ASSET_EXT:
        return True
    return False


def canonical_asset_url(url: str) -> str:
    """Drop Squarespace resize query params. Keep the path filename."""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k.lower() not in {"format"}]
    query = urllib.parse.urlencode(q)
    return urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc, parts.path, query, ""))


def safe_rel(url: str, suffix: str) -> Path:
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parts.path or "/")
    if path.endswith("/") and path != "/":
        path = path + "index"
    path = path.lstrip("/")
    if not path:
        path = "index"
    if parts.query:
        query = re.sub(r"[^A-Za-z0-9._-]+", "-", parts.query).strip("-")[:80]
        if query:
            path = f"{path}--{query}"
    # Keep the path readable and prevent escaping the raw tree.
    path = path.replace("..", "_")
    return Path(path + suffix)


def add_source(entry: dict, source: str) -> None:
    sources = entry.setdefault("sources", [])
    if source not in sources:
        sources.append(source)


def ensure_url(data: dict, url: str, source: str) -> dict:
    url = strip_query(url) if urllib.parse.urlsplit(url).query == "" else url
    # Page identity ignores tracking queries.
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "y_source"}]
    # format=json-pretty is a capture mode, not a page. format=rss is a feed.
    q = [(k, v) for k, v in q if not (k.lower() == "format" and v.lower().startswith("json"))]
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urllib.parse.urlencode(q), ""))
    urls = data["urls"]
    if url not in urls:
        urls[url] = {
            "url": url,
            "sources": [],
            "in_sitemap_xml": False,
            "lastmod": None,
            "redirect_chain": [],
            "http_status": None,
            "content_type": None,
            "final_url": None,
            "html_path": None,
            "json_path": None,
            "json_status": None,
            "error": None,
        }
    add_source(urls[url], source)
    return urls[url]


def parse_sitemap(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9",
          "image": "http://www.google.com/schemas/sitemap-image/1.1"}
    found = []
    for url_el in root.findall("s:url", ns):
        loc = url_el.findtext("s:loc", default="", namespaces=ns).strip()
        if not loc:
            continue
        images = []
        for img in url_el.findall("image:image", ns):
            iloc = img.findtext("image:loc", default="", namespaces=ns).strip()
            title = img.findtext("image:title", default="", namespaces=ns).strip()
            if iloc:
                images.append({"url": iloc, "title": title or None})
        found.append({
            "loc": loc,
            "lastmod": (url_el.findtext("s:lastmod", default="", namespaces=ns) or "").strip() or None,
            "changefreq": (url_el.findtext("s:changefreq", default="", namespaces=ns) or "").strip() or None,
            "priority": (url_el.findtext("s:priority", default="", namespaces=ns) or "").strip() or None,
            "images": images,
        })
    return found


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.assets: list[tuple[str, str | None]] = []
        self._in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        ad = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "a" and ad.get("href"):
            self.hrefs.append(ad["href"])
        if tag in {"area", "link"} and ad.get("href"):
            rel = ad.get("rel", "")
            if tag == "link" and rel and "stylesheet" in rel:
                return
            if tag == "link" and ad.get("as") in {"script", "style", "font"}:
                return
            self.hrefs.append(ad["href"])
        if tag == "form" and ad.get("action"):
            self.hrefs.append(ad["action"])
        if tag == "img":
            alt = ad.get("alt")
            for key in ("data-image", "data-src", "src"):
                if ad.get(key):
                    self.assets.append((ad[key], alt))
                    break
            if ad.get("srcset"):
                self.assets.extend((u, alt) for u in srcset_urls(ad["srcset"]))
        if tag in {"source", "video", "audio"} and ad.get("src"):
            self.assets.append((ad["src"], None))
        if tag == "iframe" and ad.get("src"):
            self.hrefs.append(ad["src"])
        style = ad.get("style") or ""
        for found in re.findall(r"url\(([^)]+)\)", style):
            self.assets.append((found.strip("\"'"), None))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)


def srcset_urls(value: str) -> list[str]:
    urls = []
    for part in value.split(","):
        bit = part.strip().split(" ")[0]
        if bit:
            urls.append(bit)
    return urls


def extract_links(html: str, base: str) -> tuple[list[str], list[tuple[str, str | None]], str | None]:
    parser = LinkExtractor()
    try:
        parser.feed(html)
    except Exception as exc:
        fail("extract-links", base, str(exc))
    hrefs = []
    for raw in parser.hrefs:
        url = norm_url(raw, base)
        if url:
            hrefs.append(url)
    assets = []
    for raw, alt in parser.assets:
        url = norm_url(raw, base)
        if url and is_asset_url(url):
            assets.append((canonical_asset_url(url), alt))
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip() or None
    return hrefs, assets, title


def header_and_footer(html: str, base: str) -> tuple[list[dict], list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    header = []
    nav = soup.select_one("nav.header-nav-list")
    if nav:
        position = 0
        for item in nav.find_all(class_="header-nav-item", recursive=False):
            link = item.find("a", href=True)
            if not link:
                continue
            position += 1
            label = re.sub(r"\s+", " ", link.get_text(" ", strip=True))
            href = norm_url(link["href"], base)
            header.append({"position": position, "label": label or None, "href": href})
    footer = []
    foot = soup.find("footer")
    if foot:
        seen = set()
        for link in foot.find_all("a", href=True):
            href = norm_url(link["href"], base)
            label = re.sub(r"\s+", " ", link.get_text(" ", strip=True)) or None
            key = (href, label)
            if key in seen:
                continue
            seen.add(key)
            footer.append({"label": label, "href": href})
    return header, footer


def record_chain(entry: dict, followed: dict) -> None:
    """http_status is the status of the requested URL, not the end of the chain."""
    chain = followed["chain"]
    entry["redirect_chain"] = [
        {"url": h["url"], "status": h["status"], "location": h["location"], "error": h["error"]}
        for h in chain
    ]
    final = followed["final"]
    if not chain:
        entry["http_status"] = 0
        entry["error"] = "no response"
        return
    entry["http_status"] = chain[0]["status"]
    entry["final_status"] = None if not final else final["status"]
    entry["content_type"] = None if not final else final["content_type"]
    entry["final_url"] = chain[-1]["url"]
    if chain[0].get("error"):
        entry["error"] = chain[0]["error"]
    elif final and final.get("error"):
        entry["error"] = final["error"]


def save_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def discover() -> None:
    data = load_discovery()
    data["started_at"] = data.get("started_at") or datetime.now(timezone.utc).isoformat()
    log("discover: sitemap")
    try:
        followed = follow(SITEMAP_URL)
        final = followed["final"]
        if not final or final["status"] != 200 or not final["body"]:
            fail("discover", SITEMAP_URL, f"sitemap status {None if not final else final['status']}")
        else:
            (ROOT / "raw").mkdir(parents=True, exist_ok=True)
            save_bytes(ROOT / "raw" / "sitemap.xml", final["body"])
            entries = parse_sitemap(final["body"])
            data["sitemap_count"] = len(entries)
            data["sitemap_redirect_chain"] = [
                {"url": h["url"], "status": h["status"], "location": h["location"]}
                for h in followed["chain"]
            ]
            for item in entries:
                loc = norm_url(item["loc"])
                if not loc:
                    continue
                entry = ensure_url(data, strip_query(loc), "sitemap")
                entry["in_sitemap_xml"] = True
                entry["lastmod"] = item["lastmod"]
                entry["sitemap_changefreq"] = item["changefreq"]
                entry["sitemap_priority"] = item["priority"]
                entry["sitemap_images"] = item["images"]
            log(f"discover: sitemap urls {len(entries)}")
    except Exception as exc:
        fail("discover", SITEMAP_URL, traceback.format_exc().splitlines()[-1])

    seeds = [ORIGIN + "/", "https://frdiplomacy.org/", "http://frdiplomacy.org/", "http://www.frdiplomacy.org/"]
    for path in PROBE_PATHS:
        seeds.append(ORIGIN + path)
    for url in list(data["urls"]):
        seeds.append(url)

    queue: list[tuple[str, str]] = []
    seen_fetch: set[str] = set()
    for url in seeds:
        nu = norm_url(url)
        if nu:
            queue.append((nu, "seed" if nu not in data["urls"] else data["urls"][nu]["sources"][0]))

    while queue:
        url, source = queue.pop(0)
        url = norm_url(url) or url
        # Assets discovered as links are recorded, not crawled as pages.
        if is_asset_url(url):
            entry = ensure_url(data, url, source)
            entry["kind"] = "asset"
            continue
        if not is_internal(url):
            continue
        key = strip_query(url) if "offset=" not in url and "format=" not in url else url
        if key in seen_fetch:
            continue
        seen_fetch.add(key)
        query = urllib.parse.urlsplit(url).query
        entry_url = url if ("offset=" in url or "format=" in query) else strip_query(url)
        entry = ensure_url(data, entry_url, source if source != "seed" else "probe")
        if source != "seed":
            add_source(entry, source)
        log(f"discover: GET {url}")
        try:
            followed = follow(url)
        except Exception as exc:
            fail("discover", url, str(exc))
            continue
        record_chain(entry, followed)
        final = followed["final"]
        if not final or not final["body"]:
            save_discovery(data)
            continue
        page_entry = entry
        page_url = url
        if entry["http_status"] in {301, 302, 303, 307, 308}:
            dest_url = entry.get("final_url") or url
            if "offset=" not in dest_url and "format=" not in urllib.parse.urlsplit(dest_url).query:
                dest_url = strip_query(dest_url)
            if is_internal(dest_url) and dest_url not in seen_fetch:
                dest = ensure_url(data, dest_url, source if source != "seed" else "probe")
                dest["http_status"] = final["status"]
                dest["final_status"] = final["status"]
                dest["final_url"] = entry.get("final_url")
                dest["content_type"] = final["content_type"]
                dest["redirect_chain"] = [{
                    "url": dest_url,
                    "status": final["status"],
                    "location": None,
                    "error": final.get("error"),
                }]
                seen_fetch.add(dest_url)
                page_entry = dest
                page_url = dest_url
            else:
                save_discovery(data)
                continue
        ctype = (final["content_type"] or "").lower()
        if "html" in ctype or final["body"][:1] == b"<" or b"<html" in final["body"][:500].lower():
            rel = safe_rel(page_url, ".html")
            path = RAW_HTML / rel
            save_bytes(path, final["body"])
            page_entry["html_path"] = str(path.relative_to(ROOT))
            try:
                html = final["body"].decode("utf-8", errors="replace")
            except Exception as exc:
                fail("discover", url, f"decode {exc}")
                save_discovery(data)
                continue
            base = page_url
            hrefs, assets, title = extract_links(html, base)
            page_entry["html_title"] = title
            page_entry["asset_refs"] = [{"url": u, "alt": a} for u, a in assets]
            internal = []
            external = []
            for href in hrefs:
                if is_asset_url(href):
                    assets.append((canonical_asset_url(href), None))
                    continue
                if is_internal(href):
                    internal.append(strip_query(href))
                else:
                    external.append(strip_query(href) if urllib.parse.urlsplit(href).query == "" else href)
            # Refresh asset refs after href scan.
            dedup_assets = []
            seen_a = set()
            for u, a in assets:
                if u in seen_a:
                    continue
                seen_a.add(u)
                dedup_assets.append({"url": u, "alt": a})
            page_entry["asset_refs"] = dedup_assets
            page_entry["outbound_internal"] = sorted(set(internal))
            page_entry["outbound_external"] = sorted(set(external))
            if not data["header_nav"]:
                header, footer = header_and_footer(html, base)
                if header:
                    data["header_nav"] = header
                    data["footer_links"] = footer
                    data["nav_source_url"] = base
            for href in internal:
                if href not in seen_fetch:
                    ensure_url(data, href, "crawl")
                    queue.append((href, "crawl"))
            # Blog listing pagination. Stop when a page adds no new internal links.
            if path_of(base) == "/blog" and "offset=" not in url:
                queue.append((ORIGIN + "/blog?offset=12", "crawl"))
                queue.append((ORIGIN + "/blog?format=rss", "crawl"))
        elif "xml" in ctype or final["body"].lstrip().startswith(b"<?xml") or b"<rss" in final["body"][:300]:
            rel = safe_rel(url, ".xml")
            path = RAW_HTML / rel
            save_bytes(path, final["body"])
            entry["html_path"] = str(path.relative_to(ROOT))
            entry["kind"] = "feed"
            try:
                text = final["body"].decode("utf-8", errors="replace")
                for loc in re.findall(r"<link>(.*?)</link>", text):
                    nu = norm_url(loc.strip())
                    if nu and is_internal(nu) and not is_asset_url(nu):
                        ensure_url(data, strip_query(nu), "rss")
                        if strip_query(nu) not in seen_fetch:
                            queue.append((strip_query(nu), "rss"))
            except Exception as exc:
                fail("discover", url, f"rss {exc}")
        elif is_asset_url(final_url) or any(final_url.lower().endswith(ext) for ext in ASSET_EXT):
            entry["kind"] = "asset"
        save_discovery(data)

    # Alternate routes: trailing slash, apex host, http. Record chains.
    # Do not crawl onward from these; they exist to document the redirect map.
    canonicals = [
        u for u, e in data["urls"].items()
        if e.get("kind") not in {"asset", "feed"} and is_internal(u) and "offset=" not in u
    ]
    for canon in canonicals:
        parts = urllib.parse.urlsplit(canon)
        if parts.netloc != "www.frdiplomacy.org" or parts.scheme != "https":
            continue
        path = parts.path or "/"
        variants = []
        if path != "/":
            toggled = path[:-1] if path.endswith("/") else path + "/"
            variants.append(urllib.parse.urlunsplit(("https", "www.frdiplomacy.org", toggled, "", "")))
        variants.extend([
            urllib.parse.urlunsplit(("https", "frdiplomacy.org", path, "", "")),
            urllib.parse.urlunsplit(("http", "www.frdiplomacy.org", path, "", "")),
            urllib.parse.urlunsplit(("http", "frdiplomacy.org", path, "", "")),
        ])
        # Dated blog posts also exist as a flat /blog/{slug} pattern, and flat
        # posts might answer on a dated path. Probe the sibling shape.
        m = re.match(r"^/blog/\d{4}/(.+)$", path)
        if m:
            variants.append(ORIGIN + "/blog/" + m.group(1))
        for variant in variants:
            if variant in data["urls"] and data["urls"][variant].get("http_status") is not None:
                continue
            entry = ensure_url(data, variant, "variant")
            log(f"discover: VARIANT {variant}")
            try:
                followed = follow(variant)
            except Exception as exc:
                fail("discover", variant, str(exc))
                continue
            record_chain(entry, followed)
            final = followed["final"]
            if final and final["body"] and entry["http_status"] == 200:
                rel = safe_rel(variant, ".html")
                path_out = RAW_HTML / rel
                if not path_out.exists():
                    save_bytes(path_out, final["body"])
                    entry["html_path"] = str(path_out.relative_to(ROOT))
                entry["body_sha256"] = hashlib.sha256(final["body"]).hexdigest()
            save_discovery(data)

    # Blog URL pattern siblings for posts already on /blog/{slug}: try nothing
    # speculative beyond the year-folder form we already saw. Hash canonical bodies.
    for url, entry in data["urls"].items():
        html_path = entry.get("html_path")
        if html_path and not entry.get("body_sha256"):
            p = ROOT / html_path
            if p.exists():
                entry["body_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    save_discovery(data)
    pages = [u for u, e in data["urls"].items() if e.get("kind") not in {"asset", "feed"}]
    log(f"discover: done urls={len(data['urls'])} page-like={len(pages)}")


def capture() -> None:
    data = load_discovery()
    targets = []
    for url, entry in data["urls"].items():
        if entry.get("kind") in {"asset", "feed"}:
            continue
        if not is_internal(url):
            continue
        # JSON for the URL that actually rendered, and for the requested URL
        # when it itself returned HTML (including 404 templates).
        if entry.get("http_status") in {301, 302, 303, 307, 308}:
            continue
        targets.append(url)
    log(f"capture: {len(targets)} json payloads")
    for url in targets:
        entry = data["urls"][url]
        json_url = url + ("&" if urllib.parse.urlsplit(url).query else "?") + "format=json-pretty"
        log(f"capture: GET {json_url}")
        try:
            followed = follow(json_url)
        except Exception as exc:
            fail("capture", json_url, str(exc))
            entry["json_status"] = 0
            entry["json_error"] = str(exc)
            save_discovery(data)
            continue
        final = followed["final"]
        entry["json_status"] = None if not final else final["status"]
        entry["json_redirect_chain"] = [
            {"url": h["url"], "status": h["status"], "location": h["location"]}
            for h in followed["chain"]
        ]
        if not final or final["body"] is None:
            fail("capture", json_url, "empty response")
            save_discovery(data)
            continue
        rel = safe_rel(url, ".json")
        path = RAW_JSON / rel
        save_bytes(path, final["body"])
        entry["json_path"] = str(path.relative_to(ROOT))
        try:
            payload = json.loads(final["body"].decode("utf-8", errors="replace"))
            entry["json_ok"] = True
            summarize_json(data, entry, payload, url)
        except Exception as exc:
            entry["json_ok"] = False
            entry["json_error"] = f"not json: {exc}"
            fail("capture", json_url, entry["json_error"])
        save_discovery(data)
    paginate_blog(data)
    fetch_missing_pages(data)
    save_discovery(data)
    log("capture: done")


def paginate_blog(data: dict) -> None:
    blog_url = ORIGIN + "/blog"
    blog = data["urls"].get(blog_url) or {}
    reported = blog.get("json_item_count_reported") or 0
    returned = blog.get("json_item_count_returned") or 0
    offset = returned
    pages = 0
    while reported and offset < reported and pages < 10:
        page_url = f"{blog_url}?offset={offset}"
        json_url = page_url + "&format=json-pretty"
        log(f"capture: GET {json_url}")
        try:
            followed = follow(json_url)
        except Exception as exc:
            fail("capture", json_url, str(exc))
            break
        final = followed["final"]
        entry = ensure_url(data, page_url, "blog-collection")
        record_chain(entry, followed)
        entry["kind"] = "feed"
        if not final or not final["body"]:
            fail("capture", json_url, "empty pagination response")
            break
        path = RAW_JSON / safe_rel(page_url, ".json")
        save_bytes(path, final["body"])
        entry["json_path"] = str(path.relative_to(ROOT))
        try:
            payload = json.loads(final["body"].decode("utf-8", errors="replace"))
        except Exception as exc:
            fail("capture", json_url, f"not json: {exc}")
            break
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            data.setdefault("notes", []).append(
                f"Blog offset {offset} returned no items. Collection itemCount is {reported}; public items returned on the first page: {returned}."
            )
            break
        before = len(data.get("blog_items") or [])
        summarize_json(data, entry, payload, page_url)
        after = len(data.get("blog_items") or [])
        if after == before:
            break
        offset += len(items)
        pages += 1
        save_discovery(data)


def fetch_missing_pages(data: dict) -> None:
    pending = [
        url for url, entry in data["urls"].items()
        if entry.get("http_status") is None and entry.get("kind") not in {"asset", "feed"} and is_internal(url)
    ]
    for url in pending:
        entry = data["urls"][url]
        log(f"capture: HTML {url}")
        try:
            followed = follow(url)
        except Exception as exc:
            fail("capture", url, str(exc))
            continue
        record_chain(entry, followed)
        final = followed["final"]
        if not final or not final["body"] or entry["http_status"] != 200:
            save_discovery(data)
            continue
        rel = safe_rel(url, ".html")
        path = RAW_HTML / rel
        save_bytes(path, final["body"])
        entry["html_path"] = str(path.relative_to(ROOT))
        html = final["body"].decode("utf-8", errors="replace")
        hrefs, assets, title = extract_links(html, url)
        entry["html_title"] = title
        entry["asset_refs"] = [{"url": u, "alt": a} for u, a in assets]
        internal, external = [], []
        for href in hrefs:
            if is_asset_url(href):
                continue
            if is_internal(href):
                internal.append(strip_query(href))
            else:
                external.append(href)
        entry["outbound_internal"] = sorted(set(internal))
        entry["outbound_external"] = sorted(set(external))
        save_discovery(data)


def ms_to_iso(value) -> str | None:
    if value in (None, "", 0):
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(ms / 1000, SITE_TZ)
    return dt.isoformat()


def summarize_json(data: dict, entry: dict, payload: dict, url: str) -> None:
    if not isinstance(payload, dict):
        return
    col = payload.get("collection") if isinstance(payload.get("collection"), dict) else {}
    entry["collection_id"] = col.get("id")
    entry["collection_title"] = col.get("title")
    entry["navigation_title"] = col.get("navigationTitle")
    entry["collection_url"] = col.get("fullUrl")
    entry["collection_type"] = col.get("typeName") or col.get("typeLabel")
    entry["collection_updated_on"] = ms_to_iso(col.get("updatedOn"))
    entry["homepage_flag"] = col.get("homepage")
    entry["show_cart"] = payload.get("showCart")
    items = payload.get("items")
    if isinstance(items, list):
        entry["json_item_count_returned"] = len(items)
        entry["json_item_count_reported"] = col.get("itemCount")
        for item in items:
            if not isinstance(item, dict):
                continue
            full = item.get("fullUrl")
            if not full:
                continue
            item_url = norm_url(full, ORIGIN)
            if not item_url:
                continue
            item_url = strip_query(item_url)
            child = ensure_url(data, item_url, "blog-collection")
            child["blog_item"] = blog_item_summary(item)
            data["blog_items"] = [
                b for b in data.get("blog_items", []) if b.get("url") != item_url
            ]
            data["blog_items"].append(child["blog_item"] | {"url": item_url})
            if item_url not in data["urls"] or data["urls"][item_url].get("http_status") is None:
                # Captured via collection; page fetch happens if not already crawled.
                pass
    # Single blog post payload uses top-level item.
    item = payload.get("item") if isinstance(payload.get("item"), dict) else None
    if item and item.get("fullUrl"):
        entry["blog_item"] = blog_item_summary(item)
    # Pull asset URLs out of JSON HTML blobs.
    blobs = []
    if isinstance(payload.get("mainContent"), str):
        blobs.append(payload["mainContent"])
        text = re.sub(r"<[^>]+>", " ", payload["mainContent"])
        text = re.sub(r"\s+", " ", text).strip()
        entry["json_main_text_chars"] = len(text)
    if item and isinstance(item.get("body"), str):
        blobs.append(item["body"])
        entry["json_body_chars"] = len(item["body"])
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("body"), str):
                blobs.append(it["body"])
    for blob in blobs:
        hrefs, assets, _title = extract_links(blob, url)
        refs = entry.setdefault("asset_refs", [])
        have = {r["url"] for r in refs}
        for asset_url, alt in assets:
            if asset_url not in have:
                refs.append({"url": asset_url, "alt": alt})
                have.add(asset_url)
        internal = set(entry.get("outbound_internal") or [])
        external = set(entry.get("outbound_external") or [])
        for href in hrefs:
            if is_asset_url(href):
                cu = canonical_asset_url(href)
                if cu not in have:
                    refs.append({"url": cu, "alt": None})
                    have.add(cu)
            elif is_internal(href):
                internal.add(strip_query(href))
            else:
                external.add(href)
        entry["outbound_internal"] = sorted(internal)
        entry["outbound_external"] = sorted(external)


def blog_item_summary(item: dict) -> dict:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "urlId": item.get("urlId"),
        "fullUrl": item.get("fullUrl"),
        "excerpt": item.get("excerpt") or None,
        "publishOn": ms_to_iso(item.get("publishOn")),
        "updatedOn": ms_to_iso(item.get("updatedOn")),
        "author": author.get("displayName") or None,
        "workflowState": item.get("workflowState"),
        "body_chars": len(item.get("body") or ""),
        "tags": item.get("tags") or [],
        "categories": item.get("categories") or [],
        "location": item.get("location") or None,
    }


def html_fragment_to_markdown(html: str, base: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "canvas"]):
        tag.decompose()
    md = render_node(soup, base).strip()
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + ("\n" if md.strip() else "")


def render_node(node, base: str) -> str:
    if isinstance(node, NavigableString):
        text = str(node).replace("\xa0", " ")
        return re.sub(r"\s+", " ", text)
    if not isinstance(node, Tag):
        return ""
    name = node.name or ""
    classes = set(node.get("class") or [])
    if name in {"script", "style", "noscript", "svg", "canvas"}:
        return ""
    if classes & {"accordion-icon-container", "visually-hidden", "header-burger", "plus"}:
        return ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        # Accordion titles are buttons inside headings. Keep the heading level.
        level = int(name[1])
        text = inline_text(node).strip()
        if not text:
            return ""
        return f"\n\n{'#' * level} {text}\n\n"
    if name == "br":
        return "\n"
    if name == "p":
        text = render_children(node, base).strip()
        return f"\n\n{text}\n\n" if text else ""
    if name == "blockquote":
        inner = html_fragment_to_markdown("".join(str(c) for c in node.children), base).strip()
        quoted = "\n".join("> " + line if line else ">" for line in inner.splitlines())
        return f"\n\n{quoted}\n\n"
    if name in {"ul", "ol"}:
        lines = []
        index = 1
        for li in node.find_all("li", recursive=False):
            item = render_children(li, base).strip()
            item = re.sub(r"\n{2,}", "\n", item)
            if name == "ol":
                lines.append(f"{index}. {item}")
                index += 1
            else:
                lines.append(f"- {item}")
        return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""
    if name == "a":
        return render_link(node, base)
    if name == "img":
        return render_image(node, base)
    if name in {"strong", "b"}:
        text = render_children(node, base).strip()
        return f"**{text}**" if text else ""
    if name in {"em", "i"}:
        text = render_children(node, base).strip()
        return f"*{text}*" if text else ""
    if name == "hr":
        return "\n\n---\n\n"
    if "accordion-item__dropdown" in classes:
        return render_children(node, base)
    return render_children(node, base)


def render_children(node: Tag, base: str) -> str:
    return "".join(render_node(child, base) for child in node.children)


def inline_text(node: Tag) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def render_link(node: Tag, base: str) -> str:
    href = node.get("href")
    imgs = node.find_all("img")
    label = inline_text(node)
    if imgs and not label:
        return "".join(render_image(img, base) for img in imgs)
    if not href:
        return render_children(node, base)
    abs_href = norm_url(href, base) or href
    if not label:
        label = abs_href
    return f"[{label}]({abs_href})"


def render_image(node: Tag, base: str) -> str:
    src = node.get("data-image") or node.get("data-src") or node.get("src")
    if not src:
        return ""
    abs_src = norm_url(src, base) or src
    alt = node.get("alt")
    if alt is None:
        alt = ""
    alt = re.sub(r"\s+", " ", alt).strip()
    return f"\n\n![{alt}]({abs_src})\n\n"


def page_main_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    article = soup.select_one("article#page-regions") or soup.select_one("article")
    if article:
        # Drop embedded site footer if the theme nested it.
        for foot in article.find_all("footer"):
            foot.decompose()
        return str(article)
    main = soup.select_one("main#page") or soup.select_one("main")
    if main:
        for foot in main.find_all("footer"):
            foot.decompose()
        return str(main)
    return str(soup.body or soup)


def firecrawl_authenticated() -> bool:
    try:
        proc = subprocess.run(
            ["npx", "--yes", "firecrawl-cli", "--status"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        fail("render", "firecrawl", f"status check failed: {exc}")
        return False
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lowered = text.lower()
    if "authenticated: false" in lowered or "not authenticated" in lowered:
        return False
    if "authenticated: true" in lowered or "authenticated via stored credentials" in lowered:
        return True
    return "authenticated" in lowered


def render() -> None:
    data = load_discovery()
    authed = firecrawl_authenticated()
    data["firecrawl_authenticated"] = authed
    if not authed:
        fail(
            "render",
            "firecrawl",
            "Firecrawl CLI is not authenticated. Rendered markdown falls back to the saved HTML.",
        )
    targets = []
    for url, entry in sorted(data["urls"].items()):
        if entry.get("kind") in {"asset", "feed"}:
            continue
        if not entry.get("html_path"):
            continue
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "www.frdiplomacy.org":
            continue
        if parts.query:
            continue
        path = parts.path or "/"
        canonical = entry.get("http_status") == 200 and (path == "/" or not path.endswith("/"))
        known_miss = path in {"/frd-method", "/frd-method/"} and entry.get("http_status") == 404
        if not canonical and not known_miss:
            # Trailing-slash twins and probe 404s still get a local markdown
            # render so the cross-check exists. Firecrawl is reserved for the
            # canonical URL and the known broken footer target.
            write_html_markdown(entry, url, RAW_MD / safe_rel(url, ".md"))
            entry["render_source"] = "html"
            entry["markdown_path"] = str((RAW_MD / safe_rel(url, ".md")).relative_to(ROOT))
            continue
        targets.append(url)
    log(f"render: {len(targets)} pages authed={authed}")
    for url in targets:
        entry = data["urls"][url]
        out = RAW_MD / safe_rel(url, ".md")
        out.parent.mkdir(parents=True, exist_ok=True)
        if authed and entry.get("render_source") == "firecrawl" and out.exists() and out.stat().st_size > 0:
            entry["markdown_path"] = str(out.relative_to(ROOT))
            continue
        if authed:
            log(f"render: firecrawl {url}")
            time.sleep(7)
            try:
                proc = subprocess.run(
                    [
                        "npx", "--yes", "firecrawl-cli", "scrape", url,
                        "--format", "markdown",
                        "--only-main-content",
                        "-o", str(out),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                    err = (proc.stderr or proc.stdout or "empty output")[-500:]
                    fail("render", url, err)
                    write_html_markdown(entry, url, out)
                    entry["render_source"] = "html"
                    entry["render_error"] = err
                else:
                    entry["render_source"] = "firecrawl"
                    entry["markdown_path"] = str(out.relative_to(ROOT))
            except Exception as exc:
                fail("render", url, str(exc))
                write_html_markdown(entry, url, out)
                entry["render_source"] = "html"
                entry["render_error"] = err
            # Stay under Firecrawl's per-minute limit. The extra sleep is
            # in addition to the site's own one-request-per-second cap.
            time.sleep(RATE_SECONDS)
        else:
            write_html_markdown(entry, url, out)
            entry["render_source"] = "html"
        entry["markdown_path"] = str(out.relative_to(ROOT)) if out.exists() else None
        save_discovery(data)
    log("render: done")


def write_html_markdown(entry: dict, url: str, out: Path) -> None:
    html_path = entry.get("html_path")
    if not html_path:
        return
    raw = (ROOT / html_path).read_text(encoding="utf-8", errors="replace")
    md = html_fragment_to_markdown(page_main_html(raw), url)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")


def collection_item(url: str) -> dict | None:
    blog_path = RAW_JSON / "blog.json"
    if not blog_path.exists():
        return None
    try:
        payload = json.loads(blog_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    want = path_of(url)
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        full = norm_url(item.get("fullUrl") or "", ORIGIN)
        if full and path_of(strip_query(full)) == want:
            return item
    return None


def load_json(entry: dict) -> dict | None:
    path = entry.get("json_path")
    if not path:
        return None
    file = ROOT / path
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return None


def meta_content(html: str, *, prop: str | None = None, name: str | None = None) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip() or None
    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag["content"].strip() or None
    return None


def html_title(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return re.sub(r"\s+", " ", soup.title.string).strip() or None
    return None


def first_h1(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not h1:
        return None
    text = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip()
    return text or None


def plain_excerpt(html: str | None) -> str | None:
    if not html or not str(html).strip():
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def is_post(url: str, entry: dict) -> bool:
    path = path_of(url)
    if entry.get("blog_item") and path.startswith("/blog/") and path != "/blog":
        return True
    if entry.get("collection_type") in {"blog-basic-grid", "blog"} and path not in {"/blog", "/blog/"}:
        item = entry.get("blog_item")
        if item and item.get("fullUrl"):
            return True
    return False


def content_filename(url: str, post: bool) -> Path:
    raw_path = urllib.parse.unquote(path_of(url))
    trailing = raw_path.endswith("/") and raw_path != "/"
    path = raw_path.strip("/")
    if not path:
        name = "index"
    else:
        name = path.replace("/", "--")
    if trailing:
        name += "--slash"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    folder = CONTENT_POSTS if post else CONTENT_PAGES
    return folder / f"{name}.md"


def yaml_frontmatter(fields: dict) -> str:
    dumped = yaml.safe_dump(
        fields,
        sort_keys=False,
        allow_unicode=True,
        width=1000,
        default_flow_style=False,
    )
    return "---\n" + dumped + "---\n\n"


def word_count(markdown: str) -> int:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", markdown)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*_`]+", " ", text)
    return len(re.findall(r"\S+", text))


def local_asset_name(url: str, used: dict[str, str]) -> str:
    parts = urllib.parse.urlsplit(url)
    base = Path(urllib.parse.unquote(parts.path)).name
    base = base.replace("+", " ")
    base = urllib.parse.unquote(base)
    base = re.sub(r"[\x00-\x1f]", "", base).strip() or "asset"
    # Keep the original name. Disambiguate collisions from different URLs.
    if base not in used:
        used[base] = url
        return base
    if used[base] == url:
        return base
    stem = Path(base).stem
    suffix = Path(base).suffix
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    name = f"{stem}-{digest}{suffix}"
    used[name] = url
    return name


def normalize() -> None:
    data = load_discovery()
    CONTENT_PAGES.mkdir(parents=True, exist_ok=True)
    CONTENT_POSTS.mkdir(parents=True, exist_ok=True)
    nav_by_href = {}
    for item in data.get("header_nav") or []:
        if item.get("href"):
            nav_by_href[strip_query(item["href"])] = item
    # Asset local names are assigned in the assets step. Normalize records
    # original URLs; the assets step rewrites frontmatter paths after download.
    written = []
    for url, entry in sorted(data["urls"].items()):
        if entry.get("kind") in {"asset", "feed"}:
            continue
        if entry.get("http_status") != 200:
            continue
        if "variant" in entry.get("sources", []) and "sitemap" not in entry.get("sources", []) and "crawl" not in entry.get("sources", []) and "blog-collection" not in entry.get("sources", []) and "rss" not in entry.get("sources", []) and "probe" not in entry.get("sources", []):
            # Variant URLs that 200 are duplicate routes. Keep a page only when
            # the body differs or the path is a known duplicate homepage.
            pass
        post = is_post(url, entry)
        payload = load_json(entry)
        html = ""
        if entry.get("html_path"):
            html = (ROOT / entry["html_path"]).read_text(encoding="utf-8", errors="replace")
        item = None
        if payload and isinstance(payload.get("item"), dict):
            item = payload["item"]
        if item is None or not (isinstance(item, dict) and (item.get("body") or "").strip()):
            alt = collection_item(url)
            if alt:
                item = alt
        # Blog collection JSON lives on /blog, not on the post. Load the post's own JSON.
        title = None
        body_html = None
        source = None
        published = None
        updated = None
        author = None
        excerpt = None
        if post and item and isinstance(item.get("body"), str) and item.get("body").strip():
            title = item.get("title") or None
            body_html = item.get("body")
            published = ms_to_iso(item.get("publishOn"))
            updated = ms_to_iso(item.get("updatedOn"))
            author_obj = item.get("author") if isinstance(item.get("author"), dict) else {}
            author = author_obj.get("displayName") or None
            excerpt = plain_excerpt(item.get("excerpt"))
            source = "json"
        elif post and entry.get("blog_item"):
            summary = entry["blog_item"]
            title = summary.get("title")
            published = summary.get("publishOn")
            updated = summary.get("updatedOn")
            author = summary.get("author")
            excerpt = plain_excerpt(summary.get("excerpt"))
        if title is None:
            title = entry.get("collection_title") or first_h1(html) or entry.get("html_title")
        if updated is None:
            updated = entry.get("collection_updated_on")
        rendered = ""
        if entry.get("markdown_path") and (ROOT / entry["markdown_path"]).exists():
            rendered = (ROOT / entry["markdown_path"]).read_text(encoding="utf-8", errors="replace")
        html_body = ""
        if html:
            html_body = html_fragment_to_markdown(page_main_html(html), url)
        if body_html:
            body = html_fragment_to_markdown(body_html, url)
            check = rendered if entry.get("render_source") == "firecrawl" and rendered.strip() else html_body
            entry["render_crosscheck"] = "firecrawl" if entry.get("render_source") == "firecrawl" else "html"
            source = "both" if entry.get("render_source") == "firecrawl" and check.strip() else "json"
            if check.strip():
                json_words = set(re.findall(r"[A-Za-z0-9’']+", body.lower()))
                check_words = re.findall(r"[A-Za-z0-9’']+", check.lower())
                missing = [w for w in check_words if w not in json_words and len(w) > 3]
                # Distinctive words present in the render and absent from the JSON body.
                if len(set(missing)) > max(20, int(len(set(check_words)) * 0.15)):
                    entry["dropped_content"] = True
        else:
            firecrawl_body = rendered.strip() + "\n" if entry.get("render_source") == "firecrawl" and rendered.strip() else ""
            fc_words = word_count(firecrawl_body) if firecrawl_body else 0
            html_words = word_count(html_body) if html_body else 0
            # Page JSON is a thin layout wrapper. Prefer Firecrawl when it
            # keeps the page, and the saved HTML when Firecrawl is much shorter.
            if firecrawl_body and (html_words == 0 or fc_words >= html_words * 0.8):
                body = firecrawl_body
                source = "firecrawl"
            else:
                body = html_body
                source = "html"
                if firecrawl_body:
                    entry["dropped_content"] = True
                    entry["firecrawl_word_count"] = fc_words
                    entry["html_word_count"] = html_words
        nav = nav_by_href.get(strip_query(url))
        parent = None
        if post:
            parent = "/blog"
        seo_title = html_title(html) if html else None
        seo_description = meta_content(html, name="description") if html else None
        if seo_description is None and html:
            seo_description = meta_content(html, prop="og:description")
        og_image = meta_content(html, prop="og:image") if html else None
        if og_image:
            og_image = norm_url(og_image, url)
        asset_urls = []
        for ref in entry.get("asset_refs") or []:
            if ref.get("url") and ref["url"] not in asset_urls:
                asset_urls.append(ref["url"])
        # Also images that survived into the markdown body.
        for found in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", body):
            nu = norm_url(found, url)
            if nu and is_asset_url(nu):
                cu = canonical_asset_url(nu)
                if cu not in asset_urls:
                    asset_urls.append(cu)
        fields = {
            "title": title,
            "slug": path_of(url),
            "url": url,
            "type": "post" if post else "page",
            "published_date": published,
            "updated_date": updated,
            "author": author,
            "excerpt": excerpt,
            "nav_label": (nav or {}).get("label") or entry.get("navigation_title") if not post else None,
            "parent": parent,
            "seo_title": seo_title,
            "seo_description": seo_description,
            "og_image": og_image,
            "assets": asset_urls,
            "source": source,
        }
        # Pages are not "in the nav" just because navigationTitle exists.
        # nav_label is the header label when the URL is in the header, else the
        # Squarespace navigationTitle when the JSON has one and the page is a
        # primary page. Posts stay null.
        if post:
            fields["nav_label"] = None
        elif nav:
            fields["nav_label"] = nav.get("label")
        else:
            fields["nav_label"] = entry.get("navigation_title") or None
        path = content_filename(url, post)
        path.write_text(yaml_frontmatter(fields) + body, encoding="utf-8")
        entry["content_path"] = str(path.relative_to(ROOT))
        entry["word_count"] = word_count(body)
        entry["content_type_kind"] = fields["type"]
        entry["title"] = title
        entry["parent"] = parent
        entry["source"] = source
        entry["seo_description"] = seo_description
        entry["seo_title"] = seo_title
        written.append(str(path.relative_to(ROOT)))
        save_discovery(data)
    log(f"normalize: wrote {len(written)} markdown files")


def image_dimensions(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".ico"}:
        return None
    try:
        from PIL import Image
        with Image.open(path) as img:
            return f"{img.width}x{img.height}"
    except Exception as exc:
        fail("assets", str(path), f"dimensions {exc}")
        return None


def assets() -> None:
    data = load_discovery()
    ASSETS.mkdir(parents=True, exist_ok=True)
    used: dict[str, str] = {}
    # Gather references: url -> {alts, pages}
    refs: dict[str, dict] = {}
    for page_url, entry in data["urls"].items():
        for ref in entry.get("asset_refs") or []:
            asset_url = canonical_asset_url(ref["url"])
            bucket = refs.setdefault(asset_url, {"alts": [], "pages": []})
            if page_url not in bucket["pages"]:
                bucket["pages"].append(page_url)
            alt = ref.get("alt")
            if alt and alt not in bucket["alts"]:
                bucket["alts"].append(alt)
        for image in entry.get("sitemap_images") or []:
            asset_url = canonical_asset_url(image["url"])
            bucket = refs.setdefault(asset_url, {"alts": [], "pages": []})
            if page_url not in bucket["pages"]:
                bucket["pages"].append(page_url)
            if image.get("title") and image["title"] not in bucket["alts"]:
                bucket["alts"].append(image["title"])
    # Logo from website JSON of any captured payload.
    for entry in data["urls"].values():
        payload = load_json(entry)
        if not payload:
            continue
        website = payload.get("website") if isinstance(payload.get("website"), dict) else {}
        logo = website.get("logoImageUrl")
        if logo:
            logo_url = norm_url(logo, ORIGIN)
            if logo_url:
                logo_url = canonical_asset_url(logo_url)
                bucket = refs.setdefault(logo_url, {"alts": [], "pages": []})
                if "site-logo" not in bucket["pages"]:
                    bucket["pages"].append("site-logo")
                alt = website.get("siteTitle")
                if alt and alt not in bucket["alts"]:
                    bucket["alts"].append(alt)
        break

    records = []
    url_to_local = {}
    for asset_url, info in sorted(refs.items()):
        name = local_asset_name(asset_url, used)
        dest = ASSETS / name
        status = None
        error = None
        if dest.exists() and dest.stat().st_size > 0:
            status = 200
            log(f"assets: exists {name}")
        else:
            log(f"assets: GET {asset_url}")
            try:
                followed = follow(asset_url)
                final = followed["final"]
                status = None if not final else final["status"]
                if final and final["status"] == 200 and final["body"]:
                    save_bytes(dest, final["body"])
                else:
                    error = None if not final else final["error"] or f"status {status}"
                    fail("assets", asset_url, error or f"status {status}")
            except Exception as exc:
                status = 0
                error = str(exc)
                fail("assets", asset_url, error)
        local = str(dest.relative_to(ROOT)) if dest.exists() else None
        url_to_local[asset_url] = local
        size = dest.stat().st_size if dest.exists() else None
        dims = image_dimensions(dest) if dest.exists() else None
        records.append({
            "original_url": asset_url,
            "local_path": local or "",
            "file_size": size if size is not None else "",
            "dimensions": dims or "",
            "alt_text": " | ".join(info["alts"]),
            "pages": " | ".join(info["pages"]),
            "http_status": status if status is not None else "",
            "error": error or "",
        })
    data["asset_records"] = records
    data["asset_local"] = {k: v for k, v in url_to_local.items() if v}
    # Rewrite content frontmatter asset lists to local paths.
    for entry in data["urls"].values():
        content = entry.get("content_path")
        if not content:
            continue
        path = ROOT / content
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        raw_fm = text[4:end]
        body = text[end + 4 :]
        if body.startswith("\n"):
            body = body[1:]
        fields = yaml.safe_load(raw_fm) or {}
        locals_ = []
        for asset_url in fields.get("assets") or []:
            local = url_to_local.get(canonical_asset_url(asset_url))
            if local and local not in locals_:
                locals_.append(local)
        fields["assets"] = locals_
        path.write_text(yaml_frontmatter(fields) + body, encoding="utf-8")
    save_discovery(data)
    write_assets_csv(records)
    log(f"assets: {len(records)} files referenced")


def write_assets_csv(records: list[dict]) -> None:
    INVENTORY.mkdir(parents=True, exist_ok=True)
    path = INVENTORY / "assets.csv"
    fields = ["original_url", "local_path", "file_size", "dimensions", "alt_text", "pages", "http_status", "error"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def check_external(urls: list[str]) -> dict[str, dict]:
    results = {}
    for url in urls:
        log(f"inventory: external {url}")
        try:
            followed = follow(url)
            final = followed["final"]
            results[url] = {
                "status": None if not final else final["status"],
                "final_url": followed["chain"][-1]["url"] if followed["chain"] else None,
                "error": None if not final else final["error"],
                "chain": [{"url": h["url"], "status": h["status"], "location": h["location"]} for h in followed["chain"]],
            }
        except Exception as exc:
            results[url] = {"status": 0, "final_url": None, "error": str(exc), "chain": []}
            fail("inventory", url, str(exc))
    return results


def depth_of(url: str) -> int:
    path = path_of(url).strip("/")
    if not path:
        return 0
    return len(path.split("/"))


def inventory() -> None:
    data = load_discovery()
    nav = {strip_query(i["href"]): i for i in (data.get("header_nav") or []) if i.get("href")}
    footer_hrefs = {strip_query(i["href"]) for i in (data.get("footer_links") or []) if i.get("href") and is_internal(i["href"])}

    external_urls = sorted({
        u for entry in data["urls"].values() for u in (entry.get("outbound_external") or [])
        if u.startswith("http")
    })
    # Skip template CDNs. They are not content links.
    external_urls = [
        u for u in external_urls
        if (urllib.parse.urlsplit(u).hostname or "") not in SKIP_ASSET_HOSTS | CONTENT_ASSET_HOSTS
        and not is_asset_url(u)
    ]
    external_status = check_external(external_urls)
    data["external_status"] = external_status

    rows = []
    for url, entry in sorted(data["urls"].items()):
        if entry.get("kind") in {"asset", "feed"}:
            continue
        nav_item = nav.get(strip_query(url))
        rows.append({
            "url": url,
            "title": entry.get("title") or entry.get("html_title") or entry.get("collection_title") or "",
            "type": entry.get("content_type_kind") or ("post" if is_post(url, entry) else "page"),
            "parent": entry.get("parent") or "",
            "nav_position": nav_item["position"] if nav_item else "",
            "word_count": entry.get("word_count") if entry.get("word_count") is not None else "",
            "depth": depth_of(url),
            "in_sitemap_xml": "y" if entry.get("in_sitemap_xml") else "n",
            "http_status": entry.get("http_status") if entry.get("http_status") is not None else "",
            "last_modified": entry.get("collection_updated_on") or entry.get("lastmod") or "",
            "outbound_internal_links": " | ".join(entry.get("outbound_internal") or []),
            "outbound_external_links": " | ".join(entry.get("outbound_external") or []),
        })
    INVENTORY.mkdir(parents=True, exist_ok=True)
    with (INVENTORY / "sitemap.csv").open("w", encoding="utf-8", newline="") as fh:
        fields = [
            "url", "title", "type", "parent", "nav_position", "word_count", "depth",
            "in_sitemap_xml", "http_status", "last_modified", "outbound_internal_links",
            "outbound_external_links",
        ]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    with (INVENTORY / "redirects.csv").open("w", encoding="utf-8", newline="") as fh:
        fields = ["url", "http_status", "redirect_target", "chain", "sources", "final_url"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for url, entry in sorted(data["urls"].items()):
            chain = entry.get("redirect_chain") or []
            target = ""
            if chain and chain[0].get("status") in {301, 302, 303, 307, 308}:
                target = chain[0].get("location") or ""
            writer.writerow({
                "url": url,
                "http_status": entry.get("http_status") if entry.get("http_status") is not None else "",
                "redirect_target": target,
                "chain": " -> ".join(
                    f"{h.get('url')} [{h.get('status')}]" for h in chain
                ),
                "sources": "|".join(entry.get("sources") or []),
                "final_url": entry.get("final_url") or "",
            })

    write_issues(data, external_status, nav, footer_hrefs)
    write_readme(data, external_status)
    save_discovery(data)
    log("inventory: done")


def page_text(entry: dict) -> str:
    parts = []
    if entry.get("content_path"):
        text = (ROOT / entry["content_path"]).read_text(encoding="utf-8", errors="replace")
        parts.append(text)
    elif entry.get("html_path"):
        parts.append((ROOT / entry["html_path"]).read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def write_issues(data: dict, external_status: dict, nav: dict, footer_hrefs: set[str]) -> None:
    lines = ["# Archive issues", ""]
    lines.append(f"Captured {data.get('updated_at') or data.get('started_at')}.")
    lines.append("")

    # Broken internal
    lines.append("## Broken internal links")
    lines.append("")
    known = data["urls"]
    broken_internal = []
    for url, entry in sorted(known.items()):
        for href in entry.get("outbound_internal") or []:
            target = known.get(href) or known.get(strip_query(href))
            status = None if not target else target.get("http_status")
            if status is None:
                # Not fetched. Note it rather than calling it broken.
                broken_internal.append((url, href, "not fetched"))
            elif status >= 400 or status == 0:
                broken_internal.append((url, href, str(status)))
    if not broken_internal:
        lines.append("No fetched internal link returned an error status.")
    else:
        for src, href, status in broken_internal:
            lines.append(f"- `{src}` links to `{href}` ({status})")
    lines.append("")

    lines.append("## Broken external links")
    lines.append("")
    any_ext = False
    for url, info in sorted(external_status.items()):
        status = info.get("status")
        if status in {404, 410} or status == 0:
            any_ext = True
            lines.append(f"- `{url}` status {status} {info.get('error') or ''}".rstrip())
    if not any_ext:
        lines.append("No external link returned 404, 410, or a connection error. Responses of 401 or 403 are listed under unchecked, because a bot block is not evidence the URL is broken.")
    lines.append("")
    lines.append("## External links that did not return success")
    lines.append("")
    for url, info in sorted(external_status.items()):
        status = info.get("status")
        if status and status >= 400 and status not in {404, 410}:
            lines.append(f"- `{url}` status {status}")
    lines.append("")

    lines.append("## Duplicate routes")
    lines.append("")
    by_hash: dict[str, list[str]] = {}
    for url, entry in data["urls"].items():
        digest = entry.get("body_sha256")
        if digest and entry.get("http_status") == 200:
            by_hash.setdefault(digest, []).append(url)
    dupes = {h: urls for h, urls in by_hash.items() if len(urls) > 1}
    if not dupes:
        lines.append("No two 200 responses shared a body hash.")
    for digest, urls in dupes.items():
        lines.append(f"- identical body `{digest[:12]}`:")
        for url in sorted(urls):
            entry = data["urls"][url]
            lines.append(f"  - `{url}` collection `{entry.get('collection_id')}` sources {', '.join(entry.get('sources') or [])}")
    lines.append("")

    lines.append("## Conflicting URL patterns")
    lines.append("")
    blog_urls = [u for u in data["urls"] if path_of(u).startswith("/blog/") and data["urls"][u].get("http_status") == 200]
    dated = [u for u in blog_urls if re.search(r"/blog/\d{4}/", path_of(u))]
    flat = [u for u in blog_urls if u not in dated and path_of(u) != "/blog"]
    lines.append("Blog posts resolve on two path shapes:")
    lines.append("")
    lines.append(f"- flat `/blog/{{slug}}` ({len(flat)} URLs)")
    for url in sorted(flat):
        lines.append(f"  - `{url}`")
    lines.append(f"- dated `/blog/{{year}}/{{slug}}` ({len(dated)} URLs)")
    for url in sorted(dated):
        lines.append(f"  - `{url}`")
    lines.append("")

    lines.append("## Posts with missing or malformed slugs")
    lines.append("")
    flagged = False
    for item in data.get("blog_items") or []:
        url_id = item.get("urlId") or ""
        title = item.get("title")
        reasons = []
        last = url_id.split("/")[-1]
        if not url_id:
            reasons.append("urlId is empty")
        if re.fullmatch(r"[a-z0-9]{16,}", last):
            reasons.append("urlId is an unedited Squarespace token with no hyphens")
        if re.search(r"(^|/)[-a-z]*post-1$", url_id):
            reasons.append("urlId ends in the placeholder post-1 and does not describe the title")
        if reasons:
            flagged = True
            lines.append(f"- `{item.get('url')}` title `{title}` urlId `{url_id}`")
            for reason in reasons:
                lines.append(f"  - {reason}")
    if not flagged:
        lines.append("No public post had an empty urlId or a token-shaped slug.")
    reported = {i.get("url") for i in data.get("blog_items") or []}
    sitemap_posts = [
        u for u, e in data["urls"].items()
        if e.get("in_sitemap_xml") and path_of(u).startswith("/blog/")
    ]
    missing_from_collection = [u for u in sitemap_posts if u not in reported]
    returned = None
    reported_count = None
    for entry in data["urls"].values():
        if entry.get("json_item_count_reported") is not None:
            reported_count = entry.get("json_item_count_reported")
            returned = entry.get("json_item_count_returned")
            break
    lines.append("")
    lines.append(f"Blog collection `itemCount` is {reported_count}. The public `items` array returned {returned}. Sitemap post URLs absent from that array: {len(missing_from_collection)}.")
    if reported_count and returned is not None and reported_count != returned:
        lines.append("Offset pagination (`/blog?offset=12&format=json-pretty`) returned no further public items during discovery. The gap is not exposed on the public collection endpoint. Unpublished items were not requested.")
    lines.append("")

    lines.append("## Orphaned pages")
    lines.append("")
    lines.append("A page is a nav orphan when it returns 200, is not the blog index or a post, and appears in neither the header nav nor the footer.")
    lines.append("")
    inbound = {}
    for url, entry in data["urls"].items():
        for href in entry.get("outbound_internal") or []:
            inbound.setdefault(strip_query(href), set()).add(url)
    for url, entry in sorted(data["urls"].items()):
        if entry.get("http_status") != 200:
            continue
        if entry.get("kind") in {"asset", "feed"}:
            continue
        if is_post(url, entry):
            continue
        path = path_of(url)
        if path in {"/blog"}:
            continue
        in_header = strip_query(url) in nav
        in_footer = strip_query(url) in footer_hrefs
        if in_header or in_footer:
            continue
        sources = ", ".join(entry.get("sources") or [])
        pointers = sorted(inbound.get(strip_query(url), []))
        lines.append(f"- `{url}` sources: {sources}")
        if pointers:
            lines.append(f"  - inbound links: {', '.join(f'`{p}`' for p in pointers)}")
        else:
            lines.append("  - no inbound internal link from a fetched page")
    lines.append("")

    lines.append("## Organization name, address, and contact details")
    lines.append("")
    address_re = re.compile(
        r"\b\d{1,6}\s+[A-Za-z0-9.'’-][^\n,]{1,40}?\s(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Suite|Ste)\b\.?",
        re.I,
    )
    city_re = re.compile(r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    phone_re = re.compile(r"(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")
    addresses: dict[str, list[str]] = {}
    emails: dict[str, list[str]] = {}
    phones: dict[str, list[str]] = {}
    for url, entry in sorted(data["urls"].items()):
        if entry.get("http_status") != 200 or not entry.get("content_path"):
            continue
        text = page_text(entry)
        for label, regex, bucket in (
            ("address", address_re, addresses),
            ("city", city_re, addresses),
            ("email", email_re, emails),
            ("phone", phone_re, phones),
        ):
            found = sorted(set(regex.findall(text)))
            if found:
                bucket[url] = found
    if not addresses:
        lines.append("No street address or City, ST ZIP pattern was found in captured page text.")
    else:
        lines.append("Address strings:")
        for url, found in addresses.items():
            lines.append(f"- `{url}`: {', '.join(found)}")
    lines.append("")
    if emails:
        lines.append("Email addresses:")
        for url, found in emails.items():
            lines.append(f"- `{url}`: {', '.join(found)}")
    else:
        lines.append("No email address was found in captured page text.")
    lines.append("")
    if phones:
        lines.append("Phone numbers:")
        for url, found in phones.items():
            lines.append(f"- `{url}`: {', '.join(found)}")
    else:
        lines.append("No phone number was found in captured page text.")
    lines.append("")
    # JSON map pins
    pins = []
    for item in data.get("blog_items") or []:
        loc = item.get("location") or {}
        if isinstance(loc, dict) and (loc.get("addressLine1") or loc.get("mapLat")):
            pins.append((item.get("url"), loc.get("addressLine1"), loc.get("addressLine2"), loc.get("mapLat"), loc.get("mapLng")))
    if pins:
        lines.append("Squarespace item location fields (empty address lines with coordinates are platform defaults, not a published street address, unless addressLine1 is set):")
        for url, line1, line2, lat, lng in pins:
            lines.append(f"- `{url}` addressLine1 `{line1}` addressLine2 `{line2}` lat `{lat}` lng `{lng}`")
    lines.append("")

    lines.append("## Forms and cart scaffolding")
    lines.append("")
    for url, entry in sorted(data["urls"].items()):
        if not entry.get("html_path"):
            continue
        html = (ROOT / entry["html_path"]).read_text(encoding="utf-8", errors="replace")
        forms = len(re.findall(r"<form\b", html, re.I))
        interesting = []
        if forms:
            interesting.append(f"{forms} form element(s)")
        low = html.lower()
        if "sqs-block-form" in low or "form-block" in low:
            interesting.append("Squarespace form block")
        if "newsletter" in low and ("form" in low or "sign up" in low):
            interesting.append("newsletter block")
        if "needs a storage option" in low:
            interesting.append("newsletter storage placeholder is visible")
        if path_of(url) in {"/cart", "/checkout", "/commerce", "/store", "/shop", "/products"}:
            interesting.append(f"commerce path status {entry.get('http_status')} showCart={entry.get('show_cart')}")
        if interesting and entry.get("http_status") in {200, 404}:
            lines.append(f"- `{url}` status {entry.get('http_status')}: {'; '.join(interesting)}")
    lines.append("")

    lines.append("## Missing alt text")
    lines.append("")
    for url, entry in sorted(data["urls"].items()):
        missing = []
        for ref in entry.get("asset_refs") or []:
            if ref.get("alt") is None or str(ref.get("alt")).strip() == "":
                missing.append(ref["url"])
        if missing:
            lines.append(f"- `{url}`")
            for asset_url in missing:
                lines.append(f"  - `{asset_url}`")
    lines.append("")

    lines.append("## Missing meta descriptions")
    lines.append("")
    for url, entry in sorted(data["urls"].items()):
        if entry.get("http_status") != 200 or not entry.get("content_path"):
            continue
        if not entry.get("seo_description"):
            lines.append(f"- `{url}`")
    lines.append("")

    lines.append("## Known issues from the prior review")
    lines.append("")
    lines.append("### Footer link /frd-method")
    lines.append("")
    method_urls = [u for u in data["urls"] if path_of(u) == "/frd-method"]
    if not method_urls:
        lines.append("Not observed. No fetched page linked to `/frd-method`, and the probe was not recorded.")
    else:
        for url in method_urls:
            entry = data["urls"][url]
            lines.append(f"- `{url}` status {entry.get('http_status')} sources {', '.join(entry.get('sources') or [])}")
        referrers = [u for u, e in data["urls"].items() if any(path_of(h) == "/frd-method" for h in (e.get("outbound_internal") or []))]
        footer_hit = any(path_of(i.get("href") or "") == "/frd-method" for i in data.get("footer_links") or [])
        lines.append(f"- Present in the parsed footer of `{data.get('nav_source_url')}`: {footer_hit}")
        lines.append("- Pages that link to it: " + (", ".join(f"`{u}`" for u in referrers) if referrers else "none"))
    lines.append("")
    lines.append("### Duplicate homepage routes")
    lines.append("")
    home_urls = [u for u in data["urls"] if path_of(urllib.parse.urlunsplit(urllib.parse.urlsplit(u)._replace(query=""))) in {"/", "/home", "/home/"}]
    for url in sorted(home_urls):
        entry = data["urls"][url]
        lines.append(
            f"- `{url}` status {entry.get('http_status')} collection `{entry.get('collection_id')}` "
            f"fullUrl `{entry.get('collection_url')}` sha `{str(entry.get('body_sha256') or '')[:12]}`"
        )
    lines.append("")
    lines.append("### Ecommerce and cart scaffolding")
    lines.append("")
    for path in ("/cart", "/checkout", "/commerce", "/store", "/shop", "/products"):
        matches = [u for u in data["urls"] if path_of(u) == path and urllib.parse.urlsplit(u).netloc == "www.frdiplomacy.org" and urllib.parse.urlsplit(u).scheme == "https"]
        if not matches:
            lines.append(f"- `{path}` was not in the fetch set")
            continue
        for url in matches:
            entry = data["urls"][url]
            lines.append(f"- `{url}` status {entry.get('http_status')} showCart `{entry.get('show_cart')}` title `{entry.get('html_title')}`")
    lines.append("")
    lines.append("### Two blog URL patterns")
    lines.append("")
    lines.append("Confirmed above under conflicting URL patterns. Both shapes are in the sitemap and return 200. They are not redirects of each other unless a variant row in redirects.csv says so.")
    lines.append("")
    lines.append("### Two posts without proper slugs")
    lines.append("")
    lines.append("See the malformed-slug section for the public items that match. The collection itemCount gap, if any, is not a third public slug.")
    lines.append("")
    lines.append("### Inconsistent physical address")
    lines.append("")
    if len(addresses) <= 1:
        lines.append("Refuted as a visible contradiction. Captured page text has fewer than two distinct address-bearing pages. Details are in the contact section above.")
    else:
        unique = {tuple(v) for v in addresses.values()}
        if len(unique) == 1:
            lines.append("The same address string appears on every page that has one.")
        else:
            lines.append("Confirmed. More than one address string is present. See the contact section above.")
    lines.append("")

    lines.append("## Capture failures")
    lines.append("")
    if FAILURES_PATH.exists():
        for line in FAILURES_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lines.append(f"- {line}")
    else:
        lines.append("No failures were logged.")
    lines.append("")
    (INVENTORY / "issues.md").write_text("\n".join(lines), encoding="utf-8")


def write_readme(data: dict, external_status: dict) -> None:
    pages = [e for e in data["urls"].values() if e.get("content_type_kind") == "page"]
    posts = [e for e in data["urls"].values() if e.get("content_type_kind") == "post"]
    assets = data.get("asset_records") or []
    asset_ok = [a for a in assets if a.get("http_status") == 200]
    asset_bad = [a for a in assets if a.get("http_status") not in {200, "200"}]
    redirects = [
        e for e in data["urls"].values()
        if e.get("redirect_chain") and e["redirect_chain"][0].get("status") in {301, 302, 303, 307, 308}
    ]
    failures = []
    if FAILURES_PATH.exists():
        failures = [ln for ln in FAILURES_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    broken_ext = [u for u, info in external_status.items() if info.get("status") in {0, 404, 410}]
    lines = [
        "# frdiplomacy.org archive",
        "",
        f"Captured {data.get('updated_at') or datetime.now(timezone.utc).isoformat()}.",
        "",
        "Read-only catalog of the Squarespace 7.1 site. Nothing on the live site was changed, and no Squarespace account was used.",
        "",
        "## What was captured",
        "",
        f"- URLs recorded: {len(data.get('urls') or {})}",
        f"- Sitemap URLs: {data.get('sitemap_count')}",
        f"- Content pages: {len(pages)}",
        f"- Content posts: {len(posts)}",
        f"- Assets referenced: {len(assets)} ({len(asset_ok)} downloaded, {len(asset_bad)} failed)",
        f"- Redirecting URLs: {len(redirects)}",
        f"- External links checked: {len(external_status)} ({len(broken_ext)} broken or unreachable)",
        "",
        "## Method",
        "",
        "1. Fetched `https://frdiplomacy.org/sitemap.xml` (it redirects to `https://www.frdiplomacy.org/sitemap.xml`) and recorded every `<loc>`.",
        "2. Crawled internal links from the homepage HTML, footer, header, and blog bodies. Probed the paths listed in `scripts/archive_site.py` (`/frd-method`, `/cart`, commerce paths, and the homepage aliases). Requested trailing-slash, apex-host, and http variants and stored the redirect chain.",
        "3. Fetched `/blog?format=json-pretty` and `/blog?offset=12&format=json-pretty`, plus `/blog?format=rss` when the crawl reached it, to enumerate public posts.",
        "4. Saved each page's rendered HTML under `raw/html/` and its `?format=json-pretty` payload under `raw/json/`.",
        f"5. Rendered markdown with {'Firecrawl (`--only-main-content`)' if data.get('firecrawl_authenticated') else 'a local HTML-to-markdown conversion of `article#page-regions`, because the Firecrawl CLI was not authenticated'}. Blog post bodies prefer the JSON `item.body` HTML.",
        "6. Wrote one markdown file per 200 page and post under `content/`, with the original wording. Asset files use the decoded original filename.",
        "7. Rate limit: one request per second.",
        "",
        "Squarespace 7.1 page JSON `mainContent` is an empty layout wrapper. Page copy comes from the rendered HTML. Post copy comes from the JSON body.",
        "",
        "Dates in frontmatter are converted from Squarespace millisecond timestamps in the site timezone `America/Los_Angeles`. A field that was not in the payload is `null`.",
        "",
        "## What failed",
        "",
    ]
    if failures:
        lines.append(f"{len(failures)} failure log lines are in `inventory/failures.jsonl` and copied in `inventory/issues.md`.")
        lines.append("")
        for line in failures[:30]:
            lines.append(f"- {line}")
        if len(failures) > 30:
            lines.append(f"- … {len(failures) - 30} more")
    else:
        lines.append("No step failures were logged.")
    lines.append("")
    (INVENTORY / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "discover", "capture", "render", "normalize", "assets", "inventory",
    }:
        print(__doc__)
        sys.exit(2)
    step = sys.argv[1]
    {"discover": discover, "capture": capture, "render": render,
     "normalize": normalize, "assets": assets, "inventory": inventory}[step]()


if __name__ == "__main__":
    main()
