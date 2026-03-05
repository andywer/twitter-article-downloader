# X Article Downloader

CLI tool to download a public X.com Article and save it as Markdown.

## What it supports

- Input URL can be:
  - `https://x.com/<user>/status/<tweet_id>`
  - `https://x.com/i/article/<article_id>`
- No login, cookies, OAuth, or API keys required.
- By default it uses only HTTPS requests + HTML/JSON parsing.
- Preserves inline media when available in rich content extraction.
- If inline media is missing, it falls back to media URLs discovered in payloads/HTML and injects them into the body in order.
- Optional headless fallback exists behind `--use-headless`.

## Install

```bash
python3 -m pip install -e .
```

## Usage

```bash
xarticle "https://x.com/rohonchain/status/2023781142663754049?s=12"
```

```bash
xarticle "https://x.com/i/article/2022988148943601665" \
  --out-dir ./articles
```

Options:

- `-o, --output`: Explicit output file path, or directory path.
- `--out-dir`: Output directory when `--output` is not set.
- `--overwrite`: Replace existing file.
- `--timeout`: HTTP timeout seconds.
- `--use-headless`: Last-resort Playwright fallback (not used by default).

## Notes

- For status URLs, the tool resolves the linked article ID first, then downloads the article.
- For status URLs, it also uses a public guest-token GraphQL fallback to retrieve article rich text when article HTML is script-blocked.
- For direct `/i/article/<id>` URLs, extraction depends on public HTML/JSON visibility; if blocked, use `--use-headless` as last resort.
- Media placement is best-effort when fallback extraction is used, because public payloads do not always include explicit text anchors for each media item.
- Default filename is generated from article title + article ID.
