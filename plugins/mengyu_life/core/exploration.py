"""Read public sources for autonomous exploration without executing page content."""

import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit
from xml.etree import ElementTree

import httpx


async def validate_public_url(url):
    """Reject non-web and private destinations, including resolved host addresses.

    Args:
        url: Absolute HTTP or HTTPS source URL.

    Raises:
        ValueError: The URL targets a local or non-public resource.
        OSError: The hostname cannot be resolved.
    """
    parsed = urlsplit(url)
    host = parsed.hostname
    if (
        parsed.scheme not in ("https", "http")
        or not host
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
        or host.lower() == "localhost"
        or host.lower().endswith((".localhost", ".local", ".internal"))
    ):
        raise ValueError("Only public HTTP(S) sources are available")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        resolved = await asyncio.get_running_loop().getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = [ipaddress.ip_address(item[4][0]) for item in resolved]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Source must resolve to public addresses")


class PageText(HTMLParser):
    """Extract visible text, title and reference links; never execute page content."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.title = []
        self.links = []
        self.skip = 0
        self.in_title = False
        self.main_depth = 0
        self.main_text = []

    def handle_starttag(self, tag, attrs):
        if tag in (
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "header",
            "footer",
            "aside",
        ):
            self.skip += 1
        if tag in ("main", "article"):
            self.main_depth += 1
        if tag == "title":
            self.in_title = True
        if not self.skip and tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in (
            "script",
            "style",
            "noscript",
            "svg",
            "nav",
            "header",
            "footer",
            "aside",
        ):
            self.skip = max(0, self.skip - 1)
        if tag in ("main", "article"):
            self.main_depth = max(0, self.main_depth - 1)
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.skip or not data.strip():
            return
        self.text.append(data.strip())
        if self.main_depth:
            self.main_text.append(data.strip())
        if self.in_title:
            self.title.append(data.strip())


async def fetch_public(client, url, headers=None):
    """Fetch a bounded public document, checking each redirect before following it.

    Args:
        client: HTTP client used for network requests.
        url: Public page URL.
        headers: Optional text-reader extraction headers.

    Returns:
        Final URL, decoded document and content type.

    Raises:
        ValueError: Destination, document size, or redirect count is invalid.
        httpx.HTTPError: The source cannot be fetched.
    """
    for _ in range(5):
        await validate_public_url(url)
        async with client.stream(
            "GET",
            url,
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 MengyuPersonalReader/1.0",
                **(headers or {}),
            },
        ) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect missing location")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            chunks = []
            length = 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > 1_000_000:
                    raise ValueError("Page exceeds reader size")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return (
                str(response.url),
                raw.decode(response.encoding or "utf-8", errors="replace"),
                response.headers.get("content-type", ""),
            )
    raise ValueError("Too many redirects")


async def search_public(client, query):
    """Search public web metadata without treating snippets as pages already read.

    Args:
        client: HTTP client used for requests.
        query: Search topic or question.

    Returns:
        Search result dictionaries with titles, URLs and snippets.

    Raises:
        ValueError: Query or search results are empty or malformed.
    """
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise ValueError("Missing search query")
    _, text, _ = await fetch_public(
        client,
        "https://www.bing.com/search?"
        + urlencode(
            {
                "format": "rss",
                "q": query,
                "cc": "us",
                "setlang": "zh-hans",
                "ensearch": "1",
            }
        ),
    )
    results = []
    for item in ElementTree.fromstring(text).findall("./channel/item")[:6]:
        url = item.findtext("link", "")
        if urlsplit(url).scheme in ("https", "http"):
            results.append(
                {
                    "title": item.findtext("title", "")[:180],
                    "url": url,
                    "snippet": re.sub("<[^>]+>", "", item.findtext("description", ""))[
                        :700
                    ],
                }
            )
    anchor = query.strip().split()[0].strip('《》“”"')
    if re.fullmatch(
        r"[\u4e00-\u9fff]{3,12}|[A-Za-z][A-Za-z0-9-]{3,30}", anchor
    ) and anchor.lower() not in {"what", "which", "where", "when", "does", "official"}:
        results = [
            item
            for item in results
            if anchor.casefold()
            in (item["title"] + " " + item["snippet"] + " " + item["url"]).casefold()
        ]
    if not results:
        raise ValueError(
            "Search returned no relevant public results; change query or use browser search"
        )
    return results


async def read_public(client, url):
    """Extract text and links from a public page for later grounded study.

    Args:
        client: HTTP client used for requests.
        url: Public page URL.

    Returns:
        Document title, final URL, visible text and candidate reference links.

    Raises:
        ValueError: The fetched document is unsupported or empty.
    """
    reader_url = None
    try:
        final, text, content_type = await fetch_public(client, url)
    except httpx.HTTPError as exc:
        if urlsplit(url).hostname == "r.jina.ai" or (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code in (401, 407)
        ):
            raise
        await validate_public_url(url)
        reader_url = "https://r.jina.ai/" + url
        _, text, content_type = await fetch_public(
            client,
            reader_url,
            headers={
                "X-Target-Selector": "main, article, [role=main]",
                "X-Remove-Selector": "nav, header, footer, aside",
            },
        )
        final = url
        if re.search(
            r"Target URL returned error [45]\d\d|^Title: (?:Error )?(?:404|403|Not Found)",
            text[:3000],
            re.IGNORECASE | re.MULTILINE,
        ):
            raise ValueError("Text reader reported an unavailable original source")
    if "html" in content_type or "<html" in text[:500].lower():
        parser = PageText()
        parser.feed(text)
        title = " ".join(parser.title)[:180] or urlsplit(final).hostname
        visible = "\n".join(parser.main_text or parser.text)
        links = list(dict.fromkeys(urljoin(final, value) for value in parser.links))[
            :15
        ]
    elif any(kind in content_type for kind in ("text/", "xml", "json")):
        title, visible, links = final, text, []
    else:
        raise ValueError("Reader supports text pages, not executable or binary files")
    if not visible.strip():
        raise ValueError("No readable page text")
    return {
        "title": title,
        "url": final,
        "text": visible[:10000],
        "links": links,
        "reader_url": reader_url,
    }
