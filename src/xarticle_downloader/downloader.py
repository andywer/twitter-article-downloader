from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


STATUS_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_]{1,15}|i/web|i)/status/(\d+)(?:/.*)?$")
ARTICLE_PATH_RE = re.compile(r"^/i/article/(\d+)$")
ARTICLE_PATH_IN_TEXT_RE = re.compile(r"/i/article/(\d+)")
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", flags=re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", flags=re.IGNORECASE)
IMG_SRC_RE = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["'][^>]*>""", flags=re.IGNORECASE)
SOURCE_SRC_RE = re.compile(r"""<source\b[^>]*\bsrc=["']([^"']+)["'][^>]*>""", flags=re.IGNORECASE)
VIDEO_POSTER_RE = re.compile(r"""<video\b[^>]*\bposter=["']([^"']+)["'][^>]*>""", flags=re.IGNORECASE)
SRCSET_RE = re.compile(r"""\bsrcset=["']([^"']+)["']""", flags=re.IGNORECASE)
ID_RE = re.compile(r"^\d{8,}$")

TITLE_KEYS = {
    "title",
    "display_title",
    "article_title",
    "headline",
    "name",
}
BODY_KEYS = {
    "plain_text",
    "article_body",
    "article_text",
    "body",
    "content",
    "text",
    "full_text",
}
RICH_KEYS = {
    "rich_text",
    "richtext",
    "content_state",
    "content",
    "body_rich_text",
    "body",
    "note_tweet_results",
    "note_tweet",
}
ID_KEYS = {"article_id", "articleid", "rest_id", "id", "id_str"}

PUBLIC_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs="
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
TWEET_RESULT_BY_REST_ID_QUERY_ID = "4PdbzTmQ5PTjz9RiureISQ"

PUNCT_ASCII_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)

IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".avif",
)
VIDEO_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".webm",
    ".m3u8",
)
MEDIA_ALT_KEYS = {
    "alt",
    "alt_text",
    "accessibility_label",
    "description",
    "title",
    "name",
}


class XArticleError(RuntimeError):
    """Raised when an article cannot be resolved or extracted."""


@dataclass
class ResolvedInput:
    original_url: str
    normalized_url: str
    kind: str
    tweet_id: str | None = None
    article_id: str | None = None


@dataclass
class ArticleResult:
    article_id: str
    article_url: str
    source_url: str
    title: str
    markdown_body: str
    downloaded_at_utc: str

    @property
    def markdown_document(self) -> str:
        body = _strip_duplicate_heading(self.title, self.markdown_body.strip())
        meta = [
            f"- Source: {self.source_url}",
            f"- Article URL: {self.article_url}",
            f"- Article ID: {self.article_id}",
            f"- Downloaded: {self.downloaded_at_utc}",
        ]
        return f"# {self.title}\n\n" + "\n".join(meta) + "\n\n---\n\n" + body + "\n"

    def default_filename(self) -> str:
        return default_output_filename(self.title, self.article_id)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.scripts: list[tuple[dict[str, str], str]] = []
        self._in_title = False
        self._script_attrs: dict[str, str] | None = None
        self._script_body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if lower_tag == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content", "")
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif lower_tag == "title":
            self._in_title = True
        elif lower_tag == "script":
            self._script_attrs = attrs_dict
            self._script_body_parts = []

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag == "title":
            self._in_title = False
        elif lower_tag == "script" and self._script_attrs is not None:
            self.scripts.append((self._script_attrs, "".join(self._script_body_parts)))
            self._script_attrs = None
            self._script_body_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._script_attrs is not None:
            self._script_body_parts.append(data)


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._li_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        if lower_tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if lower_tag == "li":
            self._parts.append("\n- ")
            self._li_depth += 1
            return
        if lower_tag in {"br"}:
            self._parts.append("\n")
            return
        if lower_tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(lower_tag[1])
            self._parts.append("\n" + ("#" * level) + " ")
            return
        if lower_tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if lower_tag == "li":
            self._li_depth = max(0, self._li_depth - 1)
        if lower_tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    def to_markdown(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


class HttpClient:
    def __init__(self, timeout: float = 20.0, user_agent: str | None = None) -> None:
        self.timeout = timeout
        self.user_agent = (
            user_agent
            or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        )

    def get_text(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise XArticleError(f"HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise XArticleError(f"Network error for {url}: {exc.reason}") from exc

    def get_json(self, url: str) -> Any:
        text = self.get_text(url)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise XArticleError(f"Expected JSON from {url}") from exc

    def post_json(self, url: str, headers: dict[str, str] | None = None, body: bytes | None = None) -> Any:
        merged_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        if headers:
            merged_headers.update(headers)
        request = urllib.request.Request(url, data=body or b"", headers=merged_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise XArticleError(f"HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise XArticleError(f"Network error for {url}: {exc.reason}") from exc
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise XArticleError(f"Expected JSON from {url}") from exc

    def resolve_final_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        header_attempts = []
        if host == "t.co" or host.endswith(".t.co"):
            # t.co can return non-redirecting responses for heavier browser headers.
            header_attempts.append({"User-Agent": "Mozilla/5.0"})
        header_attempts.append(
            {
                "User-Agent": self.user_agent,
                "Accept": "text/html,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
        )

        last_error: Exception | None = None
        last_url = url
        for headers in header_attempts:
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    final_url = response.geturl() or url
                    if final_url != url:
                        return final_url
                    last_url = final_url
            except urllib.error.HTTPError as exc:
                redirect_url = exc.geturl()
                if redirect_url and redirect_url != url:
                    return redirect_url
                last_error = exc
            except urllib.error.URLError as exc:
                last_error = exc

        if last_url and (host != "t.co" and not host.endswith(".t.co")):
            return last_url
        if last_error is not None:
            if isinstance(last_error, urllib.error.HTTPError):
                raise XArticleError(f"HTTP {last_error.code} for {url}") from last_error
            if isinstance(last_error, urllib.error.URLError):
                raise XArticleError(f"Network error for {url}: {last_error.reason}") from last_error
        return url


def download_article_markdown(
    url: str,
    timeout: float = 20.0,
    use_headless: bool = False,
) -> ArticleResult:
    """Resolve an X status/article URL and return extracted markdown."""
    client = HttpClient(timeout=timeout)
    resolved = parse_input_url(url)

    if resolved.tweet_id:
        gql_article_id, gql_title, gql_body = _extract_article_from_status_graphql(client, resolved.tweet_id)
        if gql_body:
            article_id = gql_article_id or _resolve_article_id(resolved, client)
            title = _clean_title(gql_title or f"X Article {article_id}") or f"X Article {article_id}"
            return ArticleResult(
                article_id=article_id,
                article_url=f"https://x.com/i/article/{article_id}",
                source_url=resolved.original_url,
                title=title,
                markdown_body=gql_body.strip(),
                downloaded_at_utc=dt.datetime.now(dt.timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )

    article_id = resolved.article_id or _resolve_article_id(resolved, client)
    article_url = f"https://x.com/i/article/{article_id}"

    article_html = client.get_text(article_url)
    page = _parse_page(article_html)
    payloads = _extract_json_payloads(page.scripts)

    title, body = _extract_article_from_payloads(payloads, article_id, page.meta, page.title_parts)

    if not body:
        fallback_from_html = _extract_markdown_from_html(article_html)
        if _looks_like_article_text(fallback_from_html):
            body = fallback_from_html
    if not body:
        syndication_body, syndication_title = _extract_from_syndication_tweet(client, article_id)
        if syndication_body:
            body = syndication_body
        if not title and syndication_title:
            title = syndication_title

    if not body and use_headless:
        body, headless_title = _extract_with_playwright(article_url, timeout)
        if not title and headless_title:
            title = headless_title

    if body:
        body = _inject_html_media_if_missing(body, article_html)

    if not title:
        title = f"X Article {article_id}"
    title = _clean_title(title) or f"X Article {article_id}"

    if not body:
        raise XArticleError(
            "Could not extract article body from public HTML/JSON responses. "
            "Try again later or pass --use-headless if Playwright is installed."
        )

    result = ArticleResult(
        article_id=article_id,
        article_url=article_url,
        source_url=resolved.original_url,
        title=title,
        markdown_body=body.strip(),
        downloaded_at_utc=dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    )
    return result


def parse_input_url(url: str) -> ResolvedInput:
    normalized = _normalize_url(url)
    parsed = urllib.parse.urlparse(normalized)
    host = parsed.netloc.lower()
    if host not in {
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
        "mobile.x.com",
    }:
        raise XArticleError(f"Unsupported host: {host}")

    path = parsed.path.rstrip("/")
    article_match = ARTICLE_PATH_RE.match(path)
    if article_match:
        article_id = article_match.group(1)
        return ResolvedInput(
            original_url=url,
            normalized_url=f"https://x.com/i/article/{article_id}",
            kind="article",
            article_id=article_id,
        )

    status_match = STATUS_PATH_RE.match(path)
    if status_match:
        tweet_id = status_match.group(1)
        return ResolvedInput(
            original_url=url,
            normalized_url=_normalize_status_url(parsed, tweet_id),
            kind="status",
            tweet_id=tweet_id,
        )

    raise XArticleError("URL must be an X status URL or /i/article/<id> URL")


def default_output_filename(title: str, article_id: str) -> str:
    slug = _slugify(title)
    base = slug if slug else "x-article"
    return f"{base}-{article_id}.md"


def resolve_output_path(
    explicit_output: str | None,
    output_dir: str | None,
    default_filename: str,
) -> Path:
    if explicit_output:
        raw = Path(explicit_output)
        if explicit_output.endswith(("/", "\\")):
            return raw / default_filename
        if raw.exists() and raw.is_dir():
            return raw / default_filename
        return raw

    root = Path(output_dir) if output_dir else Path.cwd()
    return root / default_filename


def write_markdown_file(path: Path, markdown: str, overwrite: bool = False) -> Path:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise XArticleError(f"Output already exists: {destination}")
    destination.write_text(markdown, encoding="utf-8")
    return destination


def _normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        parsed = urllib.parse.urlparse(f"https://{url}")
    if parsed.scheme not in {"http", "https"}:
        raise XArticleError("Only http(s) URLs are supported")
    netloc = parsed.netloc.lower()
    if not netloc:
        raise XArticleError("Invalid URL")
    return urllib.parse.urlunparse(
        ("https", netloc, parsed.path, "", parsed.query, ""),
    )


def _normalize_status_url(parsed: urllib.parse.ParseResult, tweet_id: str) -> str:
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) >= 3 and path_parts[0] != "i":
        username = path_parts[0]
        return f"https://x.com/{username}/status/{tweet_id}"
    return f"https://x.com/i/web/status/{tweet_id}"


def _resolve_article_id(resolved: ResolvedInput, client: HttpClient) -> str:
    if resolved.article_id:
        return resolved.article_id
    if not resolved.tweet_id:
        raise XArticleError("Missing tweet ID")

    status_urls = [
        resolved.normalized_url,
        f"https://x.com/i/web/status/{resolved.tweet_id}",
        f"https://x.com/i/status/{resolved.tweet_id}",
        f"https://twitter.com/i/web/status/{resolved.tweet_id}",
        f"https://twitter.com/i/status/{resolved.tweet_id}",
    ]
    seen_urls: set[str] = set()
    for status_url in status_urls:
        if status_url in seen_urls:
            continue
        seen_urls.add(status_url)
        try:
            status_html = client.get_text(status_url)
        except XArticleError:
            continue
        article_id = _find_article_id_in_text(status_html)
        if article_id and article_id != resolved.tweet_id:
            return article_id

    try:
        syndication_payload = _fetch_syndication_tweet(client, resolved.tweet_id)
    except XArticleError:
        syndication_payload = None
    if syndication_payload is not None:
        article_id = _find_article_id_in_object(syndication_payload, exclude_ids={resolved.tweet_id})
        if article_id:
            return article_id

    oembed_payloads = _fetch_oembed_tweet_candidates(client, resolved)
    for payload in oembed_payloads:
        article_id = _find_article_id_in_object(payload, exclude_ids={resolved.tweet_id})
        if article_id:
            return article_id
        links = _extract_links_from_oembed_payload(payload)
        article_id = _resolve_article_id_from_links(client, links, exclude_ids={resolved.tweet_id})
        if article_id:
            return article_id

    raise XArticleError("Could not find an article reference from the status URL")


def _extract_article_from_status_graphql(
    client: HttpClient,
    tweet_id: str,
) -> tuple[str | None, str | None, str | None]:
    try:
        payload = _fetch_tweet_result_graphql(client, tweet_id)
    except XArticleError:
        return None, None, None

    article_obj = _extract_article_object_from_tweet_result_payload(payload)
    if not isinstance(article_obj, dict):
        return None, None, None

    article_id = None
    for key in ("rest_id", "article_id", "articleId", "id", "id_str"):
        value = article_obj.get(key)
        if value and ID_RE.match(str(value)):
            article_id = str(value)
            break

    title = _find_best_title(article_obj)
    body = _render_content_state_to_markdown(article_obj.get("content_state"))
    if not body:
        body = _extract_body_markdown(article_obj)
    if not body:
        body = _extract_body_markdown(payload)
    if body:
        body = _inject_media_if_missing(body, article_obj)
    if body:
        body = _inject_media_if_missing(body, payload)

    if not _looks_like_article_text(body):
        return article_id, title, None
    return article_id, title, _normalize_markdown_text(body)


def _fetch_tweet_result_graphql(client: HttpClient, tweet_id: str) -> Any:
    guest_token = _activate_guest_token(client)

    variables = {
        "tweetId": tweet_id,
        "includePromotedContent": True,
        "withBirdwatchNotes": True,
        "withVoice": True,
        "withCommunity": True,
    }
    features = {
        "articles_preview_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
    }
    field_toggles = {
        "withArticleRichContentState": True,
        "withArticlePlainText": True,
    }
    query = urllib.parse.urlencode(
        {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
            "fieldToggles": json.dumps(field_toggles, separators=(",", ":")),
        }
    )
    url = (
        "https://x.com/i/api/graphql/"
        f"{TWEET_RESULT_BY_REST_ID_QUERY_ID}/TweetResultByRestId?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {PUBLIC_BEARER_TOKEN}",
            "x-guest-token": guest_token,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=client.timeout) as response:
            payload = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise XArticleError(f"GraphQL request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise XArticleError(f"GraphQL request failed: {exc.reason}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise XArticleError("GraphQL response was not valid JSON") from exc

    if isinstance(data, dict) and data.get("errors"):
        raise XArticleError("GraphQL returned errors for tweet lookup")
    return data


def _activate_guest_token(client: HttpClient) -> str:
    payload = client.post_json(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={
            "Authorization": f"Bearer {PUBLIC_BEARER_TOKEN}",
            "User-Agent": "Mozilla/5.0",
        },
        body=b"",
    )
    if not isinstance(payload, dict):
        raise XArticleError("Guest token activation returned unexpected response")
    token = payload.get("guest_token")
    if not token:
        raise XArticleError("Guest token activation failed")
    return str(token)


def _extract_article_object_from_tweet_result_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    tweet_result = data.get("tweetResult")
    if not isinstance(tweet_result, dict):
        return None
    result = tweet_result.get("result")
    if not isinstance(result, dict):
        return None
    article = result.get("article")
    if not isinstance(article, dict):
        return None
    article_results = article.get("article_results")
    if not isinstance(article_results, dict):
        return None
    article_obj = article_results.get("result")
    if isinstance(article_obj, dict):
        return article_obj
    return None


def _fetch_syndication_tweet(client: HttpClient, tweet_id: str) -> Any:
    url = (
        "https://cdn.syndication.twimg.com/tweet-result"
        f"?id={urllib.parse.quote(tweet_id)}&lang=en"
    )
    return client.get_json(url)


def _fetch_oembed_tweet_candidates(client: HttpClient, resolved: ResolvedInput) -> list[Any]:
    if not resolved.tweet_id:
        return []
    results: list[Any] = []
    seen: set[str] = set()
    for status_url in _build_oembed_status_urls(resolved):
        if status_url in seen:
            continue
        seen.add(status_url)
        oembed_url = (
            "https://publish.twitter.com/oembed"
            f"?omit_script=true&dnt=true&url={urllib.parse.quote(status_url, safe='')}"
        )
        try:
            payload = client.get_json(oembed_url)
        except XArticleError:
            continue
        if isinstance(payload, dict):
            results.append(payload)
    return results


def _extract_from_syndication_tweet(client: HttpClient, tweet_id: str) -> tuple[str | None, str | None]:
    try:
        payload = _fetch_syndication_tweet(client, tweet_id)
    except XArticleError:
        return None, None
    title, body = _extract_article_from_payloads([payload], tweet_id, {}, [])
    return body, title


def _build_oembed_status_urls(resolved: ResolvedInput) -> list[str]:
    if not resolved.tweet_id:
        return []

    urls = [
        resolved.normalized_url,
        f"https://x.com/i/status/{resolved.tweet_id}",
        f"https://twitter.com/i/status/{resolved.tweet_id}",
        f"https://x.com/i/web/status/{resolved.tweet_id}",
        f"https://twitter.com/i/web/status/{resolved.tweet_id}",
    ]

    normalized = urllib.parse.urlparse(resolved.normalized_url)
    path_parts = normalized.path.strip("/").split("/")
    if len(path_parts) >= 3 and path_parts[1] == "status":
        username = path_parts[0]
        urls.append(f"https://twitter.com/{username}/status/{resolved.tweet_id}")
        urls.append(f"https://x.com/{username}/status/{resolved.tweet_id}")

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _extract_links_from_oembed_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    links: list[str] = []
    for key in ("html", "url", "author_url"):
        value = payload.get(key)
        if isinstance(value, str):
            links.extend(_extract_links_from_text(value))

    deduped: list[str] = []
    seen: set[str] = set()
    for link in links:
        cleaned = link.strip()
        if not cleaned or cleaned in seen:
            continue
        deduped.append(cleaned)
        seen.add(cleaned)
    return deduped


def _extract_links_from_text(text: str) -> list[str]:
    decoded = html.unescape(text)
    links = [match.group(1) for match in HREF_RE.finditer(decoded)]
    links.extend(match.group(0) for match in URL_RE.finditer(decoded))
    return links


def _resolve_article_id_from_links(
    client: HttpClient,
    links: list[str],
    exclude_ids: set[str] | None = None,
) -> str | None:
    excluded = exclude_ids or set()
    for link in links[:12]:
        direct_id = _find_article_id_in_text(link)
        if direct_id and direct_id not in excluded:
            return direct_id

        try:
            final_url = client.resolve_final_url(link)
        except XArticleError:
            continue
        resolved_id = _find_article_id_in_text(final_url)
        if resolved_id and resolved_id not in excluded:
            return resolved_id
    return None


def _find_article_id_in_text(text: str) -> str | None:
    normalized = _normalize_slashes(text)
    percent_decoded = _iter_percent_decodes(normalized, max_rounds=3)
    for candidate in percent_decoded:
        match = ARTICLE_PATH_IN_TEXT_RE.search(candidate)
        if match:
            return match.group(1)
        for pattern in (
            r'(?i)"article_id"\s*:\s*"(\d+)"',
            r'(?i)"articleid"\s*:\s*"(\d+)"',
            r'(?i)"article_id"\s*:\s*(\d+)',
            r'(?i)"articleid"\s*:\s*(\d+)',
            r'(?i)"article"\s*:\s*\{\s*"id"\s*:\s*"(\d+)"',
            r'(?i)"article"\s*:\s*\{\s*"id"\s*:\s*(\d+)',
            r"(?i)'article_id'\s*:\s*'(\d+)'",
            r"(?i)'articleid'\s*:\s*'(\d+)'",
            r"(?i)'articleid'\s*:\s*(\d+)",
            r"(?i)'article'\s*:\s*\{\s*'id'\s*:\s*'(\d+)'",
        ):
            key_match = re.search(pattern, candidate)
            if key_match:
                return key_match.group(1)
    return None


def _find_article_id_in_object(obj: Any, exclude_ids: set[str] | None = None) -> str | None:
    excluded = exclude_ids or set()
    candidates: list[tuple[int, str]] = []

    for node in _iter_nodes(obj):
        if isinstance(node, str):
            article_id = _find_article_id_in_text(node)
            if article_id and article_id not in excluded:
                candidates.append((12, article_id))

    for path, value in _iter_keyed_values(obj):
        if isinstance(value, str):
            article_id = _find_article_id_in_text(value)
            if article_id and article_id not in excluded:
                score = 10
                if any("article" in part for part in path):
                    score += 4
                if path and path[-1] in {"expanded_url", "url", "article_url", "canonical_url"}:
                    score += 2
                candidates.append((score, article_id))
            raw = value.strip()
            if ID_RE.match(raw):
                scored = _score_numeric_id_path(path)
                if scored > 0 and raw not in excluded:
                    candidates.append((scored, raw))
        elif isinstance(value, (int, float)):
            raw = str(int(value))
            if ID_RE.match(raw):
                scored = _score_numeric_id_path(path)
                if scored > 0 and raw not in excluded:
                    candidates.append((scored, raw))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, best_id = candidates[0]
    return best_id if best_score >= 6 else None


def _parse_page(html_text: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html_text)
    parser.close()
    return parser


def _extract_json_payloads(scripts: list[tuple[dict[str, str], str]]) -> list[Any]:
    payloads: list[Any] = []
    seen: set[str] = set()
    for attrs, body in scripts:
        body = body.strip()
        if not body:
            continue
        script_type = attrs.get("type", "").lower()
        script_id = attrs.get("id", "").lower()
        candidates: list[str] = []
        if script_id == "__next_data__":
            candidates.append(body)
        elif "application/json" in script_type or "ld+json" in script_type:
            candidates.append(body)
        elif body.startswith("{") or body.startswith("["):
            candidates.append(body)
        else:
            candidates.extend(_extract_json_candidates(body, limit=2))

        for candidate in candidates:
            compact = candidate.strip()
            if not compact or compact in seen:
                continue
            parsed = _safe_json_loads(compact)
            if parsed is not None:
                payloads.append(parsed)
                seen.add(compact)
    return payloads


def _safe_json_loads(value: str) -> Any | None:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _extract_json_candidates(text: str, limit: int = 2) -> list[str]:
    candidates: list[str] = []
    idx = 0
    while idx < len(text) and len(candidates) < limit:
        if text[idx] not in "{[":
            idx += 1
            continue
        start = idx
        stack = [text[idx]]
        idx += 1
        in_string = False
        escape = False
        while idx < len(text) and stack:
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch in "{[":
                    stack.append(ch)
                elif ch == "}" and stack and stack[-1] == "{":
                    stack.pop()
                elif ch == "]" and stack and stack[-1] == "[":
                    stack.pop()
            idx += 1
        if not stack:
            candidates.append(text[start:idx])
    return candidates


def _extract_article_from_payloads(
    payloads: list[Any],
    article_id: str,
    meta_tags: dict[str, str],
    title_parts: list[str],
) -> tuple[str | None, str | None]:
    article_obj = _find_best_article_object(payloads, article_id)

    title: str | None = None
    if article_obj is not None:
        title = _find_best_title(article_obj)
    if not title:
        title = _find_best_title(payloads)
    if not title:
        title = (
            meta_tags.get("og:title")
            or meta_tags.get("twitter:title")
            or "".join(title_parts).strip()
            or None
        )
    if title:
        title = _clean_title(title)

    body: str | None = None
    if article_obj is not None:
        body = _extract_body_markdown(article_obj)
    if not body:
        body = _extract_body_markdown(payloads)
    if body:
        body = _inject_media_if_missing(body, article_obj)
    if body:
        body = _inject_media_if_missing(body, payloads)

    return title, body


def _find_best_article_object(payloads: list[Any], article_id: str) -> dict[str, Any] | None:
    best_score = -1
    best_obj: dict[str, Any] | None = None
    for payload in payloads:
        for node in _iter_nodes(payload):
            if not isinstance(node, dict):
                continue
            score = _score_candidate_article_object(node, article_id)
            if score > best_score:
                best_score = score
                best_obj = node
    return best_obj if best_score >= 4 else None


def _score_candidate_article_object(node: dict[str, Any], article_id: str) -> int:
    score = 0
    lower = {str(k).lower(): v for k, v in node.items()}
    keys = set(lower.keys())

    if any("article" in key for key in keys):
        score += 2
    if keys & TITLE_KEYS:
        score += 2
    if keys & BODY_KEYS:
        score += 3
    if keys & RICH_KEYS:
        score += 2

    for key in ID_KEYS:
        value = lower.get(key)
        if value is None:
            continue
        if str(value) == article_id:
            score += 7
        elif ID_RE.match(str(value)):
            score += 1

    for value in lower.values():
        if isinstance(value, str):
            if ARTICLE_PATH_IN_TEXT_RE.search(_normalize_slashes(value)):
                score += 4
            if len(value) > 400:
                score += 1
    return score


def _find_best_title(node: Any) -> str | None:
    candidates: list[tuple[int, str]] = []
    for path, value in _iter_keyed_values(node):
        if not isinstance(value, str):
            continue
        clean = _clean_title(value)
        if not clean:
            continue
        path_set = set(path)
        score = 0
        if path and path[-1] in TITLE_KEYS:
            score += 6
        if path_set & {"article", "article_results", "note_tweet", "note_tweet_results"}:
            score += 2
        if 5 <= len(clean) <= 180:
            score += 2
        if clean.lower().endswith(" on x"):
            score -= 3
        if clean.lower().endswith(" / x"):
            score -= 3
        candidates.append((score, clean))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best = candidates[0][1]
    return best if best else None


def _extract_body_markdown(node: Any) -> str | None:
    rich = _find_rich_text_candidate(node)
    if rich is not None:
        rendered = _render_rich_text_to_markdown(rich)
        if isinstance(rich, dict) and isinstance(rich.get("blocks"), list):
            content_state_rendered = _render_content_state_to_markdown(rich)
            if content_state_rendered and _looks_like_article_text(content_state_rendered):
                rendered = content_state_rendered
        if _looks_like_article_text(rendered):
            rich_markdown = _normalize_markdown_text(rendered)
            plain = _find_best_plain_text(node)
            if plain and _looks_like_article_text(plain):
                plain_markdown = _normalize_markdown_text(plain)
                if _markdown_contains_media(rich_markdown):
                    return rich_markdown
                if len(rich_markdown) >= int(len(plain_markdown) * 0.65):
                    return rich_markdown
                return plain_markdown
            return rich_markdown

    plain = _find_best_plain_text(node)
    if plain and _looks_like_article_text(plain):
        return _normalize_markdown_text(plain)
    return None


def _find_best_plain_text(node: Any) -> str | None:
    candidates: list[tuple[int, str]] = []
    for path, value in _iter_keyed_values(node):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if len(text) < 80:
            continue
        path_set = set(path)
        score = 0
        if path and path[-1] in BODY_KEYS:
            score += 5
        if path_set & {"article", "article_results", "note_tweet_results", "note_tweet"}:
            score += 2
        if "\n" in text:
            score += 2
        if len(text) > 400:
            score += 3
        if "http://" in text or "https://" in text:
            score -= 1
        candidates.append((score, text))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _find_rich_text_candidate(node: Any) -> Any | None:
    best: tuple[int, Any] | None = None
    for path, value in _iter_keyed_values(node):
        if not isinstance(value, (list, dict)):
            continue
        if not path:
            continue
        key = path[-1]
        if key not in RICH_KEYS and "rich" not in key and "content" not in key:
            continue
        score = 1
        if key in RICH_KEYS:
            score += 3
        if isinstance(value, list) and value:
            score += 2
        if isinstance(value, dict) and value:
            score += 1
        if best is None or score > best[0]:
            best = (score, value)
    return best[1] if best else None


def _render_rich_text_to_markdown(node: Any) -> str:
    text = _render_rich_node(node).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _render_rich_node(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float, bool)):
        return str(node)
    if isinstance(node, list):
        chunks = [_render_rich_node(item).strip() for item in node]
        chunks = [chunk for chunk in chunks if chunk]
        return "\n\n".join(chunks)
    if not isinstance(node, dict):
        return ""

    node_type = str(node.get("type") or node.get("kind") or "").lower()
    text = node.get("text")
    if isinstance(text, str) and text.strip():
        content = text.strip()
    else:
        children = (
            node.get("children")
            or node.get("content")
            or node.get("nodes")
            or node.get("items")
            or node.get("blocks")
        )
        content = _render_rich_node(children).strip()

    url = node.get("url") or node.get("href")
    if isinstance(url, str) and url and content:
        content = f"[{content}]({url})"

    if node_type in {"image", "img", "photo", "media", "video", "gif"}:
        media = _render_media_node_to_markdown(node, entity_type=node_type)
        if media:
            return media

    if node_type in {"h1", "heading1"}:
        return f"# {content}".strip()
    if node_type in {"h2", "heading2"}:
        return f"## {content}".strip()
    if node_type in {"h3", "heading3"}:
        return f"### {content}".strip()
    if node_type in {"h4", "heading4"}:
        return f"#### {content}".strip()
    if node_type in {"h5", "heading5"}:
        return f"##### {content}".strip()
    if node_type in {"h6", "heading6"}:
        return f"###### {content}".strip()
    if node_type in {"li", "list_item", "listitem"}:
        return f"- {content}".strip()
    if node_type in {"blockquote", "quote"}:
        lines = [line for line in content.splitlines() if line.strip()]
        return "\n".join(f"> {line}" for line in lines).strip()
    if node_type in {"code", "code_block", "pre"}:
        return f"```\n{content}\n```".strip()
    return content


def _render_content_state_to_markdown(content_state: Any) -> str | None:
    if not isinstance(content_state, dict):
        return None
    blocks = content_state.get("blocks")
    if not isinstance(blocks, list):
        return None
    entity_map = content_state.get("entityMap")
    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        text = block.get("text")
        if not isinstance(text, str):
            text = ""
        text = _apply_draft_entity_ranges(text, block.get("entityRanges"), entity_map).strip()

        if block_type == "atomic":
            atomic = _render_draft_atomic_block(block, entity_map)
            if atomic:
                lines.append(atomic)
            continue
        if not text:
            continue

        if block_type in {"header-one"}:
            lines.append(f"# {text}")
        elif block_type in {"header-two"}:
            lines.append(f"## {text}")
        elif block_type in {"header-three"}:
            lines.append(f"### {text}")
        elif block_type in {"header-four"}:
            lines.append(f"#### {text}")
        elif block_type in {"unordered-list-item"}:
            lines.append(f"- {text}")
        elif block_type in {"ordered-list-item"}:
            lines.append(f"1. {text}")
        elif block_type in {"blockquote"}:
            lines.extend(f"> {line}" for line in text.splitlines() if line.strip())
        else:
            lines.append(text)

    if not lines:
        return None
    text = "\n\n".join(lines)
    return _normalize_markdown_text(text)


def _apply_draft_entity_ranges(text: str, entity_ranges: Any, entity_map: Any) -> str:
    if not isinstance(text, str) or not text:
        return text
    if not isinstance(entity_ranges, list) or not entity_ranges:
        return text
    merged = text
    ranges: list[tuple[int, int, str]] = []
    for entity_range in entity_ranges:
        if not isinstance(entity_range, dict):
            continue
        key = entity_range.get("key")
        offset = entity_range.get("offset")
        length = entity_range.get("length")
        if not isinstance(offset, int) or not isinstance(length, int) or length <= 0:
            continue
        entity = _get_draft_entity(entity_map, key)
        if not isinstance(entity, dict):
            continue
        data = entity.get("data")
        if not isinstance(data, dict):
            continue
        url = data.get("url") or data.get("expanded_url") or data.get("href")
        if not isinstance(url, str) or not url:
            continue
        start = max(0, min(len(merged), offset))
        end = max(start, min(len(merged), offset + length))
        visible = merged[start:end].strip() or url
        replacement = f"[{visible}]({url})"
        ranges.append((start, end, replacement))

    for start, end, replacement in sorted(ranges, key=lambda item: item[0], reverse=True):
        merged = merged[:start] + replacement + merged[end:]
    return merged


def _render_draft_atomic_block(block: dict[str, Any], entity_map: Any) -> str | None:
    entity_ranges = block.get("entityRanges")
    if isinstance(entity_ranges, list):
        for item in entity_ranges:
            if not isinstance(item, dict):
                continue
            entity = _get_draft_entity(entity_map, item.get("key"))
            if not isinstance(entity, dict):
                continue
            rendered = _render_media_node_to_markdown(entity, entity_type=str(entity.get("type") or ""))
            if rendered:
                return rendered

    block_data = block.get("data")
    if isinstance(block_data, dict):
        rendered = _render_media_node_to_markdown(block_data)
        if rendered:
            return rendered
    return None


def _get_draft_entity(entity_map: Any, key: Any) -> dict[str, Any] | None:
    if key is None:
        return None
    if isinstance(entity_map, dict):
        direct = entity_map.get(key)
        if isinstance(direct, dict):
            return direct
        str_key = str(key)
        by_str = entity_map.get(str_key)
        if isinstance(by_str, dict):
            return by_str
    if isinstance(entity_map, list) and isinstance(key, int) and 0 <= key < len(entity_map):
        value = entity_map[key]
        if isinstance(value, dict):
            return value
    return None


def _render_media_node_to_markdown(node: Any, entity_type: str = "") -> str | None:
    image_url = _pick_best_media_url(node, kind="image")
    if image_url:
        alt = _extract_media_alt_text(node, default="image")
        return f"![{alt}]({image_url})"

    video_url = _pick_best_media_url(node, kind="video")
    if video_url:
        label = "video"
        if "gif" in entity_type.lower():
            label = "gif"
        return f"[{label}]({video_url})"

    link_url = _pick_best_media_url(node, kind="link")
    if link_url:
        return link_url
    return None


def _inject_media_if_missing(body: str | None, node: Any) -> str | None:
    if not body:
        return body
    if _markdown_contains_media(body):
        return body
    urls = _extract_embedded_media_urls(node)
    return _inject_media_from_urls(body, urls)


def _inject_html_media_if_missing(body: str | None, html_text: str) -> str | None:
    if not body:
        return body
    if _markdown_contains_media(body):
        return body
    urls = _extract_media_urls_from_html(html_text)
    return _inject_media_from_urls(body, urls)


def _inject_media_from_urls(body: str, urls: list[str]) -> str:
    if not urls:
        return body
    lines: list[str] = []
    seen_lines: set[str] = set()
    for url in urls:
        if _looks_like_image_url(url):
            line = f"![image]({url})"
            if line not in seen_lines:
                lines.append(line)
                seen_lines.add(line)
        elif _looks_like_video_url(url):
            line = f"[video]({url})"
            if line not in seen_lines:
                lines.append(line)
                seen_lines.add(line)
    if not lines:
        return body
    return _inject_media_lines_inline(body, lines)


def _inject_media_lines_inline(body: str, media_lines: list[str]) -> str:
    blocks = [chunk.strip() for chunk in re.split(r"\n{2,}", body.strip()) if chunk.strip()]
    if not blocks:
        return body
    if not media_lines:
        return body

    if len(blocks) == 1:
        media_section = "\n\n".join(media_lines)
        return f"{body.rstrip()}\n\n## Media\n\n{media_section}"

    candidate_indices = [
        idx
        for idx, block in enumerate(blocks)
        if not (
            block.startswith("#")
            or block.startswith("> ")
            or block.startswith("```")
            or re.match(r"^(?:- |\d+\. )", block)
        )
    ]
    if not candidate_indices:
        candidate_indices = list(range(len(blocks)))

    insertion_map: dict[int, list[str]] = {}
    slots = len(candidate_indices)
    count = len(media_lines)
    for i, line in enumerate(media_lines):
        raw_index = int(((i + 1) * slots) / (count + 1))
        chosen = candidate_indices[min(raw_index, slots - 1)]
        insertion_map.setdefault(chosen, []).append(line)

    out: list[str] = []
    for idx, block in enumerate(blocks):
        out.append(block)
        if idx in insertion_map:
            out.extend(insertion_map[idx])
    return "\n\n".join(out).strip()


def _extract_embedded_media_urls(node: Any, max_items: int = 10) -> list[str]:
    if node is None:
        return []
    candidates: list[str] = []
    for path, value in _iter_keyed_values(node):
        if not isinstance(value, str):
            continue
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            continue
        if not (_looks_like_image_url(url) or _looks_like_video_url(url)):
            continue
        score = _score_embedded_media_path(path, url)
        if score > 0:
            candidates.append(url)
    if not candidates:
        return []
    seen: set[str] = set()
    results: list[str] = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        results.append(url)
        if len(results) >= max_items:
            break
    return results


def _extract_media_urls_from_html(html_text: str, max_items: int = 10) -> list[str]:
    if not html_text:
        return []

    raw_values: list[str] = []
    for pattern in (IMG_SRC_RE, SOURCE_SRC_RE, VIDEO_POSTER_RE):
        raw_values.extend(match.group(1) for match in pattern.finditer(html_text))
    for match in SRCSET_RE.finditer(html_text):
        srcset = match.group(1)
        for part in srcset.split(","):
            token = part.strip().split(" ", 1)[0]
            if token:
                raw_values.append(token)

    candidates: list[str] = []
    for raw in raw_values:
        url = _normalize_extracted_url(raw)
        if not url:
            continue
        if not (_looks_like_image_url(url) or _looks_like_video_url(url)):
            continue
        score = _score_embedded_media_path(("html", "tag", "src"), url)
        if score > 0:
            candidates.append(url)
    seen: set[str] = set()
    results: list[str] = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        results.append(url)
        if len(results) >= max_items:
            break
    return results


def _normalize_extracted_url(raw: str) -> str | None:
    value = html.unescape(raw).strip().strip("'\"")
    if not value:
        return None
    value = value.replace("\\/", "/")
    if value.startswith("//"):
        value = f"https:{value}"
    if not value.startswith(("http://", "https://")):
        return None
    return value


def _score_embedded_media_path(path: tuple[str, ...], url: str) -> int:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    pth = parsed.path.lower()
    leaf = path[-1] if path else ""
    score = 0

    if _looks_like_image_url(url):
        score += 8
    if _looks_like_video_url(url):
        score += 8
    if "pbs.twimg.com" in host and "/media/" in pth:
        score += 10
    if "video.twimg.com" in host:
        score += 10
    if any(token in leaf for token in ("media", "image", "photo", "thumb", "video", "original")):
        score += 6
    if any(token in pth for token in ("/profile_images/", "/emoji/", "/hashflags/")):
        score -= 12
    return score


def _pick_best_media_url(node: Any, kind: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for path, value in _iter_keyed_values(node):
        if not isinstance(value, str):
            continue
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            continue
        score = _score_media_url_candidate(path, url, kind)
        if score > 0:
            candidates.append((score, url))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _score_media_url_candidate(path: tuple[str, ...], url: str, kind: str) -> int:
    leaf = path[-1] if path else ""
    url_lower = url.lower()
    host = urllib.parse.urlparse(url).netloc.lower()
    path_lower = urllib.parse.urlparse(url).path.lower()

    if kind == "image":
        score = 0
        if _looks_like_image_url(url):
            score += 10
        if leaf in {
            "media_url_https",
            "media_url",
            "image_url",
            "image",
            "original_img_url",
            "original_image_url",
            "thumbnail_url",
            "thumb_url",
            "preview_image_url",
        }:
            score += 10
        if any(token in leaf for token in ("image", "photo", "thumb", "media_url")):
            score += 5
        if "pbs.twimg.com/media/" in url_lower:
            score += 10
        if "twimg.com" in host:
            score += 2
        if any(token in path_lower for token in ("/profile_images/", "/emoji/")):
            score -= 8
        return score

    if kind == "video":
        score = 0
        if _looks_like_video_url(url):
            score += 10
        if leaf in {"video_url", "stream_url", "playback_url"}:
            score += 8
        if any(token in leaf for token in ("video", "playback", "stream")):
            score += 5
        if "video.twimg.com" in host:
            score += 6
        return score

    score = 0
    if leaf in {"url", "expanded_url", "href", "src"}:
        score += 5
    if "x.com/" in url_lower or "twitter.com/" in url_lower:
        score += 2
    return score


def _looks_like_image_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    lower_url = url.lower()
    lower_path = parsed.path.lower()
    if any(token in lower_path for token in ("/profile_images/", "/emoji/", "/hashflags/")):
        return False
    if lower_path.endswith(IMAGE_EXTENSIONS):
        return True
    if "pbs.twimg.com/media/" in lower_url:
        return True
    if host == "pbs.twimg.com" and any(
        token in lower_path
        for token in ("/card_img/", "/amplify_video_thumb/", "/tweet_video_thumb/", "/ext_tw_video_thumb/")
    ):
        return True
    if "twimg.com" in host and (
        "/media/" in lower_path
        or "/card_img/" in lower_path
        or "/amplify_video_thumb/" in lower_path
        or "/tweet_video_thumb/" in lower_path
        or "/ext_tw_video_thumb/" in lower_path
    ):
        return True
    if "twimg.com" in host and "format=" in parsed.query.lower():
        return True
    return False


def _looks_like_video_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if path.endswith(VIDEO_EXTENSIONS):
        return True
    if host == "video.twimg.com" or host.endswith(".video.twimg.com"):
        return True
    return False


def _extract_media_alt_text(node: Any, default: str) -> str:
    candidates: list[str] = []
    for path, value in _iter_keyed_values(node):
        if not isinstance(value, str):
            continue
        if path and path[-1] in MEDIA_ALT_KEYS:
            text = re.sub(r"\s+", " ", value).strip()
            if text:
                candidates.append(text)
    if candidates:
        best = max(candidates, key=len)
        return best[:120].strip()
    return default


def _markdown_contains_media(text: str) -> bool:
    return bool(re.search(r"!\[[^\]]*]\(https?://[^)]+\)", text))


def _extract_markdown_from_html(html_text: str) -> str:
    fragment = _extract_article_fragment(html_text)
    parser = _HTMLTextExtractor()
    parser.feed(fragment)
    parser.close()
    return parser.to_markdown()


def _extract_article_fragment(html_text: str) -> str:
    for pattern in (
        r"<article\b[^>]*>.*?</article>",
        r"<main\b[^>]*>.*?</main>",
    ):
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(0)
    return html_text


def _extract_with_playwright(url: str, timeout_seconds: float) -> tuple[str | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        raise XArticleError(
            "Headless fallback requested but Playwright is not installed. "
            "Install playwright and browser binaries, then retry with --use-headless."
        )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=int(timeout_seconds * 1000))
            html_text = page.content()
            browser.close()
    except Exception as exc:
        raise XArticleError(f"Headless fallback failed: {exc}") from exc

    parsed_page = _parse_page(html_text)
    title = (
        parsed_page.meta.get("og:title")
        or parsed_page.meta.get("twitter:title")
        or "".join(parsed_page.title_parts).strip()
        or None
    )
    body = _extract_markdown_from_html(html_text)
    if body and _looks_like_article_text(body):
        return body, title
    return None, title


def _iter_nodes(node: Any) -> Any:
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_nodes(value)


def _iter_keyed_values(node: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(node, dict):
        for key, value in node.items():
            key_str = str(key).lower()
            next_path = path + (key_str,)
            yield next_path, value
            yield from _iter_keyed_values(value, next_path)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_keyed_values(value, path)


def _normalize_slashes(text: str) -> str:
    value = text
    value = value.replace("\\u002F", "/").replace("\\u002f", "/")
    value = value.replace("\\/", "/")
    value = value.replace("%2F", "/").replace("%2f", "/")
    return value


def _iter_percent_decodes(text: str, max_rounds: int = 3) -> list[str]:
    seen = {text}
    values = [text]
    current = text
    for _ in range(max_rounds):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            break
        if decoded not in seen:
            seen.add(decoded)
            values.append(decoded)
        current = decoded
    return values


def _score_numeric_id_path(path: tuple[str, ...]) -> int:
    if not path:
        return 0
    score = 0
    leaf = path[-1]
    path_set = set(path)
    if leaf in {"article_id", "articleid"}:
        score += 14
    if "article" in leaf:
        score += 7
    if leaf in {"rest_id", "id", "id_str"}:
        score += 2
    if any("article" in part for part in path):
        score += 6
    if path_set & {"card", "binding_values", "value", "legacy"}:
        score += 1
    return score


def _clean_title(title: str) -> str:
    text = html.unescape(title).strip()
    text = text.translate(PUNCT_ASCII_TRANSLATION)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+/+\s+X$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+on\s+X$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+/+\s+Twitter$", "", text, flags=re.IGNORECASE)
    text = text.strip(" -|")
    if len(text) > 240:
        text = text[:240].rstrip()
    return text


def _slugify(text: str, max_len: int = 80) -> str:
    text = _clean_title(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        return ""
    return text[:max_len].strip("-")


def _normalize_markdown_text(text: str) -> str:
    value = text.translate(PUNCT_ASCII_TRANSLATION)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _strip_duplicate_heading(title: str, body: str) -> str:
    lines = [line for line in body.splitlines()]
    if not lines:
        return body
    first = lines[0].lstrip("#").strip().lower()
    if first == title.strip().lower():
        return "\n".join(lines[1:]).lstrip()
    return body


def _looks_like_article_text(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 60:
        return False
    lower = stripped.lower()
    known_error_phrases = (
        "something went wrong, but don’t fret",
        "something went wrong, but don't fret",
        "some privacy related extensions may cause issues on x.com",
        "scriptloadfailure",
        "looking for this?",
    )
    if any(phrase in lower for phrase in known_error_phrases):
        return False
    words = re.findall(r"\w+", stripped)
    return len(words) >= 12
