from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from xarticle_downloader.downloader import (  # noqa: E402
    _extract_article_from_payloads,
    _extract_article_object_from_tweet_result_payload,
    _extract_links_from_oembed_payload,
    _find_article_id_in_object,
    _find_article_id_in_text,
    _looks_like_article_text,
    _normalize_markdown_text,
    _render_content_state_to_markdown,
    _resolve_article_id_from_links,
    default_output_filename,
    parse_input_url,
)


class ParseInputUrlTests(unittest.TestCase):
    def test_parses_status_url(self) -> None:
        resolved = parse_input_url("https://x.com/example/status/2023781142663754049?s=12")
        self.assertEqual(resolved.kind, "status")
        self.assertEqual(resolved.tweet_id, "2023781142663754049")
        self.assertEqual(
            resolved.normalized_url,
            "https://x.com/example/status/2023781142663754049",
        )

    def test_parses_i_status_url(self) -> None:
        resolved = parse_input_url("https://x.com/i/status/2023781142663754049")
        self.assertEqual(resolved.kind, "status")
        self.assertEqual(resolved.tweet_id, "2023781142663754049")

    def test_parses_article_url(self) -> None:
        resolved = parse_input_url("http://x.com/i/article/2022988148943601665")
        self.assertEqual(resolved.kind, "article")
        self.assertEqual(resolved.article_id, "2022988148943601665")
        self.assertEqual(
            resolved.normalized_url,
            "https://x.com/i/article/2022988148943601665",
        )


class ExtractionHelpersTests(unittest.TestCase):
    def test_extract_article_id_from_escaped_text(self) -> None:
        html_blob = '{"expanded_url":"https:\\/\\/x.com\\/i\\/article\\/2022988148943601665"}'
        self.assertEqual(_find_article_id_in_text(html_blob), "2022988148943601665")

    def test_extract_article_id_from_nested_article_object(self) -> None:
        payload = {
            "tweet": {
                "id_str": "2023781142663754049",
                "card": {
                    "legacy": {
                        "binding_values": {
                            "article": {"id": "2022988148943601665"},
                        }
                    }
                },
            }
        }
        found = _find_article_id_in_object(payload, exclude_ids={"2023781142663754049"})
        self.assertEqual(found, "2022988148943601665")

    def test_extract_article_id_from_percent_encoded_text(self) -> None:
        text = "url=https%3A%2F%2Fx.com%2Fi%2Farticle%2F2022988148943601665"
        self.assertEqual(_find_article_id_in_text(text), "2022988148943601665")

    def test_extract_links_from_oembed_payload(self) -> None:
        payload = {
            "html": (
                '<blockquote><p><a href="https://t.co/mzc1xbkxtQ">https://t.co/mzc1xbkxtQ</a></p>'
                '<a href="https://twitter.com/RohOnChain/status/2023781142663754049">Feb 17</a></blockquote>'
            ),
            "url": "https://twitter.com/RohOnChain/status/2023781142663754049",
        }
        links = _extract_links_from_oembed_payload(payload)
        self.assertIn("https://t.co/mzc1xbkxtQ", links)

    def test_resolve_article_id_from_links(self) -> None:
        class FakeClient:
            def resolve_final_url(self, url: str) -> str:
                if url == "https://t.co/mzc1xbkxtQ":
                    return "https://x.com/i/article/2022988148943601665"
                return url

        article_id = _resolve_article_id_from_links(
            FakeClient(),  # type: ignore[arg-type]
            ["https://twitter.com/RohOnChain/status/2023781142663754049", "https://t.co/mzc1xbkxtQ"],
            exclude_ids={"2023781142663754049"},
        )
        self.assertEqual(article_id, "2022988148943601665")

    def test_extract_article_from_payloads(self) -> None:
        article_id = "2022988148943601665"
        payload = {
            "article_results": {
                "result": {
                    "rest_id": article_id,
                    "title": "Protocol Acceleration: Builder Notes",
                    "plain_text": (
                        "We shipped three infrastructure changes this month.\n\n"
                        "First, we rebuilt indexing to avoid duplicate jobs.\n\n"
                        "Second, we switched to deterministic snapshots for replay."
                    ),
                }
            }
        }
        title, body = _extract_article_from_payloads([payload], article_id, {}, [])
        self.assertEqual(title, "Protocol Acceleration: Builder Notes")
        self.assertIsNotNone(body)
        assert body is not None
        self.assertIn("infrastructure changes", body)

    def test_default_output_filename(self) -> None:
        name = default_output_filename("Protocol Acceleration: Builder Notes", "2022988148943601665")
        self.assertEqual(name, "protocol-acceleration-builder-notes-2022988148943601665.md")

    def test_extract_article_object_from_graphql_payload(self) -> None:
        payload = {
            "data": {
                "tweetResult": {
                    "result": {
                        "article": {
                            "article_results": {
                                "result": {"rest_id": "2022988148943601665", "title": "Demo"}
                            }
                        }
                    }
                }
            }
        }
        article = _extract_article_object_from_tweet_result_payload(payload)
        self.assertIsNotNone(article)
        assert article is not None
        self.assertEqual(article.get("rest_id"), "2022988148943601665")

    def test_render_content_state_to_markdown(self) -> None:
        content_state = {
            "blocks": [
                {"type": "header-two", "text": "Section"},
                {"type": "unstyled", "text": "First paragraph."},
                {"type": "unordered-list-item", "text": "Item one"},
            ],
            "entityMap": {},
        }
        rendered = _render_content_state_to_markdown(content_state)
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("## Section", rendered)
        self.assertIn("- Item one", rendered)

    def test_error_page_not_classified_as_article(self) -> None:
        bad = (
            "Something went wrong, but don’t fret — let’s give it another shot.\n\n"
            "Some privacy related extensions may cause issues on x.com."
        )
        self.assertFalse(_looks_like_article_text(bad))

    def test_normalize_markdown_converts_smart_quotes(self) -> None:
        text = "I’m Roan — \"don’t\""
        normalized = _normalize_markdown_text(text)
        self.assertEqual(normalized, "I'm Roan - \"don't\"")


if __name__ == "__main__":
    unittest.main()
