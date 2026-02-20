from __future__ import annotations

import argparse
import sys

from .downloader import (
    XArticleError,
    download_article_markdown,
    resolve_output_path,
    write_markdown_file,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xarticle",
        description=(
            "Download a public X.com Article from either a status URL or /i/article/<id> URL "
            "and save it as Markdown."
        ),
    )
    parser.add_argument(
        "url",
        help="X status URL or X article URL",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output path (.md). If this points to a directory, default filename is used inside it.",
    )
    parser.add_argument(
        "--out-dir",
        help="Directory for output when --output is not provided (default: current directory).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds (default: 20).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output file.",
    )
    parser.add_argument(
        "--use-headless",
        action="store_true",
        help=(
            "If extraction from public HTML/JSON fails, attempt a Playwright headless fallback. "
            "Not used by default."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = download_article_markdown(
            args.url,
            timeout=args.timeout,
            use_headless=args.use_headless,
        )
        destination = resolve_output_path(
            explicit_output=args.output,
            output_dir=args.out_dir,
            default_filename=result.default_filename(),
        )
        written = write_markdown_file(destination, result.markdown_document, overwrite=args.overwrite)
    except XArticleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(str(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
