from __future__ import annotations

import pytest

from knowledge_agent.chunking import ArticleFormatError, chunk_article, parse_article


def test_parses_zenn_front_matter_and_normalizes_markdown() -> None:
    article = parse_article(
        "articles/nested/azure-agent.md",
        """---\r
title: "Azure Agent"\r
emoji: "robot"\r
type: tech\r
topics: [azure, python]\r
published: true\r
published_at: 2026-08-10T09:00:00+09:00\r
---\r
\r
# Overview\r
\r
Body.\r
""",
    )

    assert article.slug == "azure-agent"
    assert article.title == "Azure Agent"
    assert article.article_type == "tech"
    assert article.topics == ("azure", "python")
    assert article.published is True
    assert article.published_at == "2026-08-10T00:00:00Z"
    assert article.body == "# Overview\n\nBody."


def test_accepts_zenn_naive_published_at_as_jst() -> None:
    # Zenn writes "YYYY-MM-DD HH:MM" with no offset. Requiring one silently dropped a real
    # article from the index, so the JST interpretation is pinned here.
    article = parse_article(
        "articles/a.md",
        "---\n"
        'title: "T"\n'
        "emoji: robot\n"
        "type: tech\n"
        "topics: [azure]\n"
        "published: true\n"
        "published_at: 2025-04-15 17:00\n"
        "---\n"
        "Body",
    )

    assert article.published_at == "2025-04-15T08:00:00Z"


def test_accepts_date_only_published_at_as_jst_midnight() -> None:
    # YAML parses an unquoted "YYYY-MM-DD" front matter value as a date, not a datetime.
    # It carries no offset either, so it must get the same JST interpretation as a naive
    # string (test above), not UTC.
    article = parse_article(
        "articles/a.md",
        "---\n"
        'title: "T"\n'
        "emoji: robot\n"
        "type: tech\n"
        "topics: [azure]\n"
        "published: true\n"
        "published_at: 2025-04-15\n"
        "---\n"
        "Body",
    )

    assert article.published_at == "2025-04-14T15:00:00Z"


@pytest.mark.parametrize(
    "front_matter",
    [
        "emoji: robot\ntype: tech\ntopics: [azure]\npublished: true",
        "title: T\nemoji: robot\ntype: invalid\ntopics: [azure]\npublished: true",
        "title: T\nemoji: robot\ntype: tech\ntopics: azure\npublished: true",
    ],
)
def test_rejects_missing_or_invalid_front_matter(front_matter: str) -> None:
    with pytest.raises(ArticleFormatError):
        parse_article("articles/a.md", f"---\n{front_matter}\n---\nBody")


def test_rejects_duplicate_front_matter_keys() -> None:
    with pytest.raises(ArticleFormatError, match="valid YAML"):
        parse_article(
            "articles/a.md",
            """---
title: First
title: Second
emoji: a
type: tech
topics: [a]
published: true
---
Body
""",
        )


def test_chunks_by_heading_with_bounded_overlap_and_ignores_fenced_headings() -> None:
    article = parse_article(
        "articles/a.md",
        """---
title: A
emoji: book
type: tech
topics: [python]
published: false
---
# First

Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda.

```markdown
# Not a section
```

## Second

Second section text.
""",
    )

    chunks = chunk_article(article, max_chars=55, overlap_chars=12)

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(len(chunk.text) <= 55 for chunk in chunks)
    assert chunks[0].heading == "First"
    assert sum(chunk.heading == "Not a section" for chunk in chunks) == 0
    assert chunks[-1].heading == "Second"
    assert "Second section text." in chunks[-1].text


def test_rejects_empty_body_and_invalid_chunk_bounds() -> None:
    with pytest.raises(ArticleFormatError, match="body"):
        parse_article(
            "articles/a.md",
            "---\ntitle: A\nemoji: a\ntype: tech\ntopics: [a]\npublished: true\n---\n",
        )

    article = parse_article(
        "articles/a.md",
        "---\ntitle: A\nemoji: a\ntype: tech\ntopics: [a]\npublished: true\n---\nBody",
    )
    with pytest.raises(ValueError, match="overlap"):
        chunk_article(article, max_chars=10, overlap_chars=10)


def test_long_block_uses_exact_sliding_overlap() -> None:
    article = parse_article(
        "articles/a.md",
        "---\ntitle: A\nemoji: a\ntype: tech\ntopics: [a]\npublished: true\n---\n"
        + "0123456789ABCDEFGHIJ",
    )

    chunks = chunk_article(article, max_chars=10, overlap_chars=3)

    assert [chunk.text for chunk in chunks] == ["0123456789", "789ABCDEFG", "EFGHIJ"]
    assert [chunk.index for chunk in chunks] == [0, 1, 2]


def test_heading_keeps_non_closing_hash_character() -> None:
    article = parse_article(
        "articles/a.md",
        "---\ntitle: A\nemoji: a\ntype: tech\ntopics: [a]\npublished: true\n---\n"
        "# C#\n\nBody",
    )

    assert chunk_article(article)[0].heading == "C#"
