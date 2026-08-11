"""Strict Zenn front matter parsing and deterministic Markdown chunking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import PurePosixPath
from typing import Any

import yaml

CHUNK_MAX_CHARS = 1600
CHUNK_OVERLAP_CHARS = 200

_HEADING_PATTERN = re.compile(r"^ {0,3}#{1,6}\s+(?P<heading>.+?)\s*$")
_FENCE_PATTERN = re.compile(r"^\s*(?P<marker>`{3,}|~{3,})")


class ArticleFormatError(ValueError):
    """Raised when an article cannot safely enter the index."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, *, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class Article:
    slug: str
    title: str
    emoji: str
    article_type: str
    topics: tuple[str, ...]
    published: bool
    published_at: str | None
    source_path: str
    body: str


@dataclass(frozen=True, slots=True)
class ArticleChunk:
    index: int
    heading: str | None
    text: str


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArticleFormatError(f"{field} must be a non-empty string")
    return value.strip()


def _published_at(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=UTC)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ArticleFormatError("published_at must be an ISO 8601 timestamp") from error
    else:
        raise ArticleFormatError("published_at must be an ISO 8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArticleFormatError("published_at must include a timezone")
    normalized = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return normalized


def _normalized_markdown(raw: str) -> str:
    if not isinstance(raw, str):
        raise ArticleFormatError("article content must be text")
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def parse_article(path: str, raw: str) -> Article:
    normalized = _normalized_markdown(raw)
    lines = normalized.splitlines()
    if not lines or lines[0] != "---":
        raise ArticleFormatError("article must start with YAML front matter")
    try:
        closing_index = lines.index("---", 1)
    except ValueError as error:
        raise ArticleFormatError("front matter closing delimiter is missing") from error
    try:
        metadata = yaml.load(  # noqa: S506 - the loader derives from SafeLoader
            "\n".join(lines[1:closing_index]), Loader=_UniqueKeySafeLoader
        )
    except yaml.YAMLError as error:
        raise ArticleFormatError("front matter is not valid YAML") from error
    if not isinstance(metadata, dict):
        raise ArticleFormatError("front matter must be an object")

    title = _non_empty_string(metadata.get("title"), "title")
    emoji = _non_empty_string(metadata.get("emoji"), "emoji")
    article_type = _non_empty_string(metadata.get("type"), "type")
    if article_type not in {"tech", "idea"}:
        raise ArticleFormatError("type must be 'tech' or 'idea'")
    raw_topics = metadata.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ArticleFormatError("topics must be a non-empty array")
    topics = tuple(_non_empty_string(topic, "topics item") for topic in raw_topics)
    published = metadata.get("published")
    if not isinstance(published, bool):
        raise ArticleFormatError("published must be a boolean")

    source_path = PurePosixPath(path)
    if (
        source_path.is_absolute()
        or ".." in source_path.parts
        or len(source_path.parts) < 2
        or source_path.parts[0] != "articles"
        or source_path.suffix != ".md"
        or not source_path.stem
    ):
        raise ArticleFormatError("source path must identify an articles Markdown file")
    body = "\n".join(lines[closing_index + 1 :]).strip()
    if not body:
        raise ArticleFormatError("article body must not be empty")
    return Article(
        slug=source_path.stem,
        title=title,
        emoji=emoji,
        article_type=article_type,
        topics=topics,
        published=published,
        published_at=_published_at(metadata.get("published_at")),
        source_path=source_path.as_posix(),
        body=body,
    )


def _sections(body: str) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    sections: list[tuple[str | None, tuple[str, ...]]] = []
    heading: str | None = None
    content: list[str] = []
    fence_marker: str | None = None
    for line in body.splitlines():
        fence = _FENCE_PATTERN.match(line)
        if fence:
            marker = fence.group("marker")
            if fence_marker is None:
                fence_marker = marker
            elif marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                fence_marker = None
            content.append(line)
            continue
        heading_match = _HEADING_PATTERN.match(line) if fence_marker is None else None
        if heading_match:
            if content or heading is not None:
                sections.append((heading, tuple(content)))
            raw_heading = heading_match.group("heading").strip()
            heading = re.sub(r"\s+#+\s*$", "", raw_heading).strip()
            content = []
        else:
            content.append(line)
    if content or heading is not None:
        sections.append((heading, tuple(content)))
    return tuple(sections)


def _blocks(lines: tuple[str, ...]) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    fence_marker: str | None = None
    for line in lines:
        fence = _FENCE_PATTERN.match(line)
        if fence:
            marker = fence.group("marker")
            if fence_marker is None:
                fence_marker = marker
            elif marker[0] == fence_marker[0] and len(marker) >= len(fence_marker):
                fence_marker = None
            current.append(line)
            continue
        if not line.strip() and fence_marker is None:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return tuple(block for block in blocks if block)


def _split_long_block(block: str, size: int, overlap: int) -> tuple[str, ...]:
    pieces: list[str] = []
    start = 0
    while start < len(block):
        end = min(start + size, len(block))
        pieces.append(block[start:end])
        if end == len(block):
            break
        start = end - overlap
    return tuple(pieces)


def _chunk_blocks(blocks: tuple[str, ...], size: int, overlap: int) -> tuple[str, ...]:
    expanded: list[str] = []
    for block in blocks:
        if len(block) <= size:
            expanded.append(block)
        else:
            expanded.extend(_split_long_block(block, size, overlap))

    chunks: list[str] = []
    current: list[str] = []
    for block in expanded:
        candidate = "\n\n".join((*current, block))
        if current and len(candidate) > size:
            chunks.append("\n\n".join(current))
            carry: list[str] = []
            carry_size = 0
            for previous in reversed(current):
                added = len(previous) + (2 if carry else 0)
                if carry_size + added > overlap:
                    break
                carry.insert(0, previous)
                carry_size += added
            current = carry
            candidate = "\n\n".join((*current, block))
            if current and len(candidate) > size:
                current = []
        current.append(block)
    if current:
        chunks.append("\n\n".join(current))
    return tuple(chunks)


def chunk_article(
    article: Article,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> tuple[ArticleChunk, ...]:
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if type(overlap_chars) is not int or not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must satisfy 0 <= overlap_chars < max_chars")

    chunks: list[ArticleChunk] = []
    for heading, lines in _sections(article.body):
        blocks = _blocks(lines)
        if not blocks and heading is not None:
            blocks = (heading,)
        for text in _chunk_blocks(blocks, max_chars, overlap_chars):
            chunks.append(ArticleChunk(index=len(chunks), heading=heading, text=text))
    if not chunks:
        raise ArticleFormatError("article body must contain indexable text")
    return tuple(chunks)
