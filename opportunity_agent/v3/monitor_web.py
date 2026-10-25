"""Bounded static HTML retrieval. DNS resolution is pinned to the TLS socket."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

PARSER_VERSION = "monitor-html-1"
LIMIT = 2 * 1024 * 1024


class PageError(ValueError):
    pass


def safe_url(url, hosts):
    if len(url)>2000:
        raise PageError("URL_TOO_LONG")
    p = urlsplit(url)
    host = (p.hostname or "").encode("idna").decode().lower().rstrip(".")
    if (p.scheme != "https" or not host or host not in hosts or p.username or p.password
            or p.port not in (None, 443) or any(c in url for c in "\r\n\\")):
        raise PageError("UNSAFE_URL")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise PageError("PRIVATE_ADDRESS")
    except ValueError as exc:
        if isinstance(exc, PageError):
            raise
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise PageError("PRIVATE_ADDRESS")
    return urlunsplit(("https", host, p.path or "/", p.query, ""))


class _Parser(HTMLParser):
    skip = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "template", "iframe", "head"}
    boundaries = {"p", "div", "li", "tr", "section", "article", "main", "br", "h1", "h2", "h3", "h4", "h5", "h6"}
    void = {"br", "hr", "img", "input", "link", "meta", "source", "wbr", "area", "embed", "param", "col", "base"}

    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.stack, self.parts, self.blocks, self.links = url, [], [], [], []
        self.heading, self.in_heading = "", False

    def flush(self):
        value = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        self.parts = []
        if value:
            if self.in_heading:
                self.heading = value[:200]
            while value:
                boundary = value.rfind(" ",0,1200) if len(value)>1200 else len(value)
                if boundary<=0:
                    boundary = min(len(value),1200)
                self.blocks.append({"id": f"b{len(self.blocks)}", "heading": self.heading, "text": value[:boundary]})
                value = value[boundary:].lstrip()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ignored = bool(self.stack and self.stack[-1][1]) or tag in self.skip or "hidden" in attrs or attrs.get("aria-hidden") == "true"
        ignored |= attrs.get("role") in {"navigation","menu","contentinfo"}
        ignored |= tag=="header" and not any(t in {"main","article"} for t,_ in self.stack)
        noise = str(attrs.get("class", "")) + " " + str(attrs.get("id", ""))
        ignored |= bool(re.search(r"(?:^|[\s_-])(?:cookie|consent|breadcrumb|navigation|nav|navbar|menu|social|utility|share)(?:$|[\s_-])", noise, re.I))
        ignored |= bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style", ""), re.I))
        if not ignored:
            if tag in self.boundaries:
                self.flush()
            if re.fullmatch("h[1-6]", tag):
                self.in_heading = True
            if tag == "a" and attrs.get("href"):
                link = urljoin(self.url, attrs["href"])
                p = urlsplit(link)
                if p.scheme == "https":
                    link = urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))
                    self.links.append(link)
            if tag in {"td", "th"}:
                self.parts.append(" | ")
        if tag not in self.void:
            self.stack.append((tag, ignored))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        ignored = bool(self.stack and self.stack[-1][1])
        if not ignored and tag in self.boundaries:
            self.flush()
        if re.fullmatch("h[1-6]", tag):
            self.in_heading = False
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


@dataclass
class PageSnapshot:
    url: str
    html: str
    blocks: list
    links: list
    content_hash: str
    parser_version: str = PARSER_VERSION
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    etag: str | None = None
    modified: str | None = None


def parse_html(html, url, **metadata):
    parser = _Parser(url)
    parser.feed(html)
    parser.flush()
    text = " ".join(b["text"] for b in parser.blocks)
    if len(text) < 60:
        raise PageError("UNSUPPORTED_OR_EMPTY_HTML")
    if re.search(r"verify you are human|checking your browser|just a moment|access denied|temporarily unavailable|under maintenance|验证码|网站维护", text[:1500], re.I):
        raise PageError("CHALLENGE_OR_MAINTENANCE")
    links = sorted(set(parser.links))
    digest = hashlib.sha256(json.dumps([parser.blocks, links], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return PageSnapshot(url, html, parser.blocks, links, digest, **metadata)


def page_diff(before, after):
    a, b = before.blocks, after.blocks
    changes = []
    matcher = SequenceMatcher(None, [(x["heading"], x["text"]) for x in a], [(x["heading"], x["text"]) for x in b], autojunk=False)
    for kind, i, j, k, l in matcher.get_opcodes():
        if kind != "equal":
            for offset in range(0,max(j-i,l-k),4):
                changes.append({"kind": kind, "before": a[max(0,min(j,i+offset)-1):min(len(a),i+offset+5,j+1)],
                                "after": b[max(0,min(l,k+offset)-1):min(len(b),k+offset+5,l+1)]})
    if before.links != after.links:
        removed,added = sorted(set(before.links)-set(after.links)),sorted(set(after.links)-set(before.links))
        for offset in range(0,max(len(removed),len(added)),4):
            changes.append({"kind":"links","before":removed[offset:offset+4],"after":added[offset:offset+4]})
    return changes


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request(url, hosts, headers):
    current = url
    deadline = time.monotonic()+15
    for _ in range(5):
        if time.monotonic()>=deadline:
            raise PageError("FETCH_TIMEOUT")
        current = safe_url(current, hosts)
        p = urlsplit(current)
        addresses = {item[4][0] for item in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise PageError("PRIVATE_ADDRESS")
        connection = _PinnedHTTPS(p.hostname, sorted(addresses)[0], max(.01,deadline-time.monotonic()))
        try:
            connection.request("GET", urlunsplit(("", "", p.path or "/", p.query, "")), headers={
                "User-Agent": "ApplyOpsMonitor/1.0", "Accept": "text/html,text/plain", "Accept-Encoding": "identity", **headers})
            response = connection.getresponse()
            if response.status in {301,302,303,307,308}:
                current = urljoin(current, response.getheader("Location", ""))
                continue
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise PageError("UNSUPPORTED_ENCODING")
            chunks,size = [],0
            while size<=LIMIT:
                remaining = deadline-time.monotonic()
                if remaining<=0:
                    raise PageError("FETCH_TIMEOUT")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536,LIMIT+1-size))
                if not chunk:
                    break
                chunks.append(chunk);size+=len(chunk)
            body = b"".join(chunks)
            if len(body) > LIMIT:
                raise PageError("PAGE_TOO_LARGE")
            return current, response.status, dict((k.lower(), v) for k,v in response.getheaders()), body
        finally:
            connection.close()
    raise PageError("TOO_MANY_REDIRECTS")


class HttpFetcher:
    def __init__(self):
        self.robots = {}

    async def fetch(self, url, hosts, etag=None, modified=None):
        url = safe_url(url, hosts)
        host = urlsplit(url).hostname
        if host not in self.robots:
            _, status, _, body = await asyncio.wait_for(asyncio.to_thread(_request, f"https://{host}/robots.txt", hosts, {}), 15)
            if status not in {200,404,410}:
                raise PageError("ROBOTS_UNAVAILABLE")
            robot = RobotFileParser()
            robot.parse(body.decode("utf-8", errors="replace").splitlines() if status == 200 else [])
            self.robots[host] = robot if status == 200 else None
        if self.robots[host] and not self.robots[host].can_fetch("ApplyOpsMonitor", url):
            raise PageError("ROBOTS_DISALLOWED")
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if modified:
            headers["If-Modified-Since"] = modified
        final, status, meta, body = await asyncio.wait_for(asyncio.to_thread(_request, url, hosts, headers), 15)
        # Check final robots policy as well; redirects cannot bypass exclusions.
        if final != url:
            final_host = urlsplit(final).hostname
            if final_host != host or (self.robots[host] and not self.robots[host].can_fetch("ApplyOpsMonitor", final)):
                raise PageError("REDIRECT_REQUIRES_EXPLICIT_SOURCE")
        if status == 304:
            return None
        if status != 200:
            raise PageError(f"HTTP_{status}")
        if "text/html" not in meta.get("content-type", "").lower():
            raise PageError("UNSUPPORTED_CONTENT_TYPE")
        charset = re.search(r"charset=([\w-]+)", meta.get("content-type", ""), re.I)
        try:
            html = body.decode(charset[1] if charset else "utf-8", errors="replace")
        except LookupError:
            raise PageError("UNSUPPORTED_CHARSET") from None
        return parse_html(html, final, etag=meta.get("etag"), modified=meta.get("last-modified"))


class FixtureLoader:
    """Offline evaluator adapter only; never exposed by HTTP APIs."""
    def __init__(self, root):
        self.root = Path(root).resolve()

    def load(self, relative_path, original_url):
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() not in {".html", ".htm"}:
            raise PageError("INVALID_FIXTURE_PATH")
        if path.stat().st_size > LIMIT:
            raise PageError("PAGE_TOO_LARGE")
        return parse_html(path.read_text(encoding="utf-8"), original_url)
