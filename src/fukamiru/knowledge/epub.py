"""Safe, dependency-free EPUB text and structure extraction."""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree as ET

from fukamiru.errors import FukamiruError

MAX_EPUB_FILE_BYTES = 512 * 1024 * 1024
MAX_EPUB_ENTRIES = 10_000
MAX_EPUB_ENTRY_BYTES = 128 * 1024 * 1024
MAX_EPUB_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
_CONTAINER_PATH = "META-INF/container.xml"
_BLOCK_ROLES = {
    "h1": "title",
    "h2": "heading",
    "h3": "heading",
    "h4": "heading",
    "h5": "heading",
    "h6": "heading",
    "p": "paragraph",
    "li": "list_item",
    "blockquote": "quote",
    "dt": "heading",
    "dd": "paragraph",
    "figcaption": "paragraph",
}
_HIDDEN_TAGS = {"head", "script", "style", "template", "svg", "nav"}


@dataclass(frozen=True)
class EPUBBlock:
    text: str
    role: str


@dataclass(frozen=True)
class EPUBPage:
    href: str
    title: str | None
    text: str
    blocks: tuple[EPUBBlock, ...]


@dataclass(frozen=True)
class EPUBDocument:
    title: str | None
    language: str | None
    pages: tuple[EPUBPage, ...]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _archive_path(base: str, href: str) -> str:
    href_path = unquote(urldefrag(href)[0]).replace("\\", "/")
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), href_path))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        raise FukamiruError(f"EPUB contains an unsafe resource path: {href}")
    return joined


def _root_archive_path(href: str) -> str:
    href_path = unquote(urldefrag(href)[0]).replace("\\", "/")
    normalized = posixpath.normpath(href_path)
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        raise FukamiruError(f"EPUB contains an unsafe resource path: {href}")
    return normalized


def _decode_markup(raw: bytes, resource: str) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        prefix = raw[:1024].decode("ascii", errors="ignore")
        match = re.search(
            r"(?:encoding\s*=|charset\s*=)\s*['\"]?([A-Za-z0-9._-]+)",
            prefix,
            flags=re.IGNORECASE,
        )
        encoding = match.group(1) if match else "utf-8"
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise FukamiruError(
            f"EPUB text encoding could not be decoded for {resource}: {encoding}"
        ) from exc


class _ChapterParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.current_tag: str | None = None
        self.current_parts: list[str] = []
        self.blocks: list[EPUBBlock] = []
        self.visible_parts: list[str] = []
        self.image_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in _HIDDEN_TAGS:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "img":
            self.image_count += 1
        if tag in _BLOCK_ROLES:
            self._finish_block()
            self.current_tag = tag
        elif tag == "br" and self.current_tag:
            self.current_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1
            return
        if not self.hidden_depth and tag == self.current_tag:
            self._finish_block()

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.visible_parts.append(data)
            if self.current_tag:
                self.current_parts.append(data)

    def close(self) -> None:
        super().close()
        self._finish_block()

    def _finish_block(self) -> None:
        if self.current_tag:
            text = re.sub(r"\s+", " ", "".join(self.current_parts)).strip()
            if text:
                self.blocks.append(EPUBBlock(text=text, role=_BLOCK_ROLES[self.current_tag]))
        self.current_tag = None
        self.current_parts = []


class _NavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nav_depth = 0
        self.href: str | None = None
        self.parts: list[str] = []
        self.labels: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): value or "" for name, value in attrs}
        if tag.casefold() == "nav":
            nav_type = values.get("epub:type", values.get("type", "")).casefold()
            if self.nav_depth or "toc" in nav_type:
                self.nav_depth += 1
        elif self.nav_depth and tag.casefold() == "a" and values.get("href"):
            self.href = values["href"]
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self.href:
            label = re.sub(r"\s+", " ", "".join(self.parts)).strip()
            if label:
                self.labels.setdefault(self.href, label)
            self.href = None
            self.parts = []
        elif tag == "nav" and self.nav_depth:
            self.nav_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.href:
            self.parts.append(data)


class EPUBExtractor:
    """Extract spine-ordered EPUB chapters as page-like structural boundaries."""

    def extract(self, path: Path) -> EPUBDocument:
        if path.stat().st_size > MAX_EPUB_FILE_BYTES:
            raise FukamiruError(
                f"EPUB file is too large ({path.stat().st_size} bytes; "
                f"limit {MAX_EPUB_FILE_BYTES})"
            )
        try:
            with zipfile.ZipFile(path) as archive:
                self._validate_archive(archive)
                opf_path = self._opf_path(archive)
                opf_root = self._parse_xml(archive.read(opf_path), opf_path)
                manifest = self._manifest(opf_root, opf_path)
                spine = self._spine(opf_root)
                encrypted = self._encrypted_resources(archive)
                labels = self._navigation_labels(archive, manifest, opf_path)
                title = self._metadata_value(opf_root, "title")
                language = self._metadata_value(opf_root, "language")
                pages: list[EPUBPage] = []
                images = 0
                for item_id in spine:
                    item = manifest.get(item_id)
                    if item is None:
                        raise FukamiruError(f"EPUB spine references missing item: {item_id}")
                    href, media_type, _properties = item
                    resource = _archive_path(opf_path, href)
                    if resource in encrypted:
                        raise FukamiruError(
                            f"EPUB DRM/encryption is unsupported for spine resource: {resource}"
                        )
                    if media_type not in {"application/xhtml+xml", "text/html"}:
                        continue
                    try:
                        markup = _decode_markup(archive.read(resource), resource)
                    except KeyError as exc:
                        raise FukamiruError(
                            f"EPUB spine resource is missing: {resource}"
                        ) from exc
                    parser = _ChapterParser()
                    parser.feed(markup)
                    parser.close()
                    images += parser.image_count
                    blocks = list(parser.blocks)
                    if not blocks:
                        fallback = re.sub(r"\s+", " ", "".join(parser.visible_parts)).strip()
                        if fallback:
                            blocks.append(EPUBBlock(fallback, "paragraph"))
                    chapter_title = self._chapter_title(labels, opf_path, href, blocks)
                    if blocks and chapter_title and not (
                        blocks and blocks[0].role in {"title", "heading"}
                        and blocks[0].text == chapter_title
                    ):
                        blocks.insert(0, EPUBBlock(chapter_title, "heading"))
                    text = "\n\n".join(block.text for block in blocks).strip()
                    if text:
                        pages.append(EPUBPage(resource, chapter_title, text, tuple(blocks)))
        except zipfile.BadZipFile as exc:
            raise FukamiruError(f"EPUB archive is corrupt or invalid: {exc}") from exc
        except (OSError, RuntimeError) as exc:
            raise FukamiruError(f"EPUB extraction failed: {exc}") from exc
        if not pages:
            if images:
                raise FukamiruError(
                    "EPUB_IMAGE_ONLY: EPUB contains images but no extractable body text; "
                    "EPUB OCR is not supported"
                )
            raise FukamiruError("EPUB contains no extractable body text")
        return EPUBDocument(title=title, language=language, pages=tuple(pages))

    @staticmethod
    def _validate_archive(archive: zipfile.ZipFile) -> None:
        entries = archive.infolist()
        if len(entries) > MAX_EPUB_ENTRIES:
            raise FukamiruError(f"EPUB contains too many ZIP entries: {len(entries)}")
        total = 0
        for entry in entries:
            if entry.flag_bits & 0x1:
                raise FukamiruError(f"EPUB ZIP encryption is unsupported: {entry.filename}")
            pure = PurePosixPath(entry.filename.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise FukamiruError(f"EPUB contains an unsafe ZIP entry: {entry.filename}")
            if entry.file_size > MAX_EPUB_ENTRY_BYTES:
                raise FukamiruError(f"EPUB entry is too large: {entry.filename}")
            total += entry.file_size
            if total > MAX_EPUB_UNCOMPRESSED_BYTES:
                raise FukamiruError("EPUB uncompressed content exceeds the safety limit")
        if _CONTAINER_PATH not in archive.namelist():
            raise FukamiruError("EPUB is missing META-INF/container.xml")
        if "mimetype" in archive.namelist():
            media_type = archive.read("mimetype").decode("ascii", errors="replace").strip()
            if media_type != "application/epub+zip":
                raise FukamiruError(f"Invalid EPUB mimetype: {media_type}")

    @staticmethod
    def _parse_xml(raw: bytes, resource: str) -> ET.Element:
        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise FukamiruError(f"EPUB XML is malformed in {resource}: {exc}") from exc

    def _opf_path(self, archive: zipfile.ZipFile) -> str:
        root = self._parse_xml(archive.read(_CONTAINER_PATH), _CONTAINER_PATH)
        for node in root.iter():
            if _local_name(node.tag) == "rootfile" and node.get("full-path"):
                value = posixpath.normpath(str(node.get("full-path")).replace("\\", "/"))
                if value.startswith("../") or value.startswith("/"):
                    raise FukamiruError(f"EPUB package path is unsafe: {value}")
                if value not in archive.namelist():
                    raise FukamiruError(f"EPUB package document is missing: {value}")
                return value
        raise FukamiruError("EPUB container does not identify a package document")

    @staticmethod
    def _manifest(root: ET.Element, opf_path: str) -> dict[str, tuple[str, str, str]]:
        items: dict[str, tuple[str, str, str]] = {}
        for node in root.iter():
            if _local_name(node.tag) != "item":
                continue
            item_id, href = node.get("id"), node.get("href")
            if item_id and href:
                _archive_path(opf_path, href)
                items[item_id] = (
                    href,
                    node.get("media-type", ""),
                    node.get("properties", ""),
                )
        return items

    @staticmethod
    def _spine(root: ET.Element) -> list[str]:
        result = [
            str(node.get("idref"))
            for node in root.iter()
            if _local_name(node.tag) == "itemref" and node.get("idref")
        ]
        if not result:
            raise FukamiruError("EPUB package has no readable spine")
        return result

    def _encrypted_resources(self, archive: zipfile.ZipFile) -> set[str]:
        encryption_path = "META-INF/encryption.xml"
        if encryption_path not in archive.namelist():
            return set()
        root = self._parse_xml(archive.read(encryption_path), encryption_path)
        encrypted: set[str] = set()
        for node in root.iter():
            if _local_name(node.tag) == "CipherReference" and node.get("URI"):
                encrypted.add(_root_archive_path(str(node.get("URI"))))
        return encrypted

    def _navigation_labels(
        self,
        archive: zipfile.ZipFile,
        manifest: dict[str, tuple[str, str, str]],
        opf_path: str,
    ) -> dict[str, str]:
        labels: dict[str, str] = {}
        for href, media_type, properties in manifest.values():
            is_epub3_nav = "nav" in properties.split() and media_type == "application/xhtml+xml"
            is_epub2_ncx = media_type == "application/x-dtbncx+xml"
            if not is_epub3_nav and not is_epub2_ncx:
                continue
            resource = _archive_path(opf_path, href)
            try:
                raw = archive.read(resource)
            except KeyError as exc:
                raise FukamiruError(f"EPUB navigation resource is missing: {resource}") from exc
            if is_epub3_nav:
                parser = _NavigationParser()
                parser.feed(_decode_markup(raw, resource))
                parser.close()
                for target, label in parser.labels.items():
                    labels[_archive_path(resource, target)] = label
            else:
                root = self._parse_xml(raw, resource)
                for nav_point in root.iter():
                    if _local_name(nav_point.tag) != "navPoint":
                        continue
                    target = next(
                        (
                            child.get("src")
                            for child in nav_point.iter()
                            if _local_name(child.tag) == "content" and child.get("src")
                        ),
                        None,
                    )
                    label = next(
                        (
                            child.text.strip()
                            for child in nav_point.iter()
                            if _local_name(child.tag) == "text"
                            and child.text
                            and child.text.strip()
                        ),
                        None,
                    )
                    if target and label:
                        labels[_archive_path(resource, target)] = re.sub(r"\s+", " ", label)
        return labels

    @staticmethod
    def _metadata_value(root: ET.Element, name: str) -> str | None:
        for node in root.iter():
            if _local_name(node.tag).casefold() == name and node.text and node.text.strip():
                return re.sub(r"\s+", " ", node.text).strip()
        return None

    @staticmethod
    def _chapter_title(
        labels: dict[str, str],
        opf_path: str,
        href: str,
        blocks: list[EPUBBlock],
    ) -> str | None:
        resource = _archive_path(opf_path, href)
        if resource in labels:
            return labels[resource]
        for block in blocks:
            if block.role in {"title", "heading"}:
                return block.text
        return None
