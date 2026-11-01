"""Read an EPUB's contents and sentence text. No Qt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from miraspeak.modules.api import split_sentences

_SKIP = frozenset({"script", "style"})


class EpubError(Exception):
    """The file is not a readable EPUB."""


@dataclass(frozen=True)
class Chapter:
    label: str
    sentences: tuple[str, ...]


@dataclass(frozen=True)
class EpubBook:
    title: str
    chapters: tuple[Chapter, ...]


def load_epub(path: str | Path) -> EpubBook:
    """Contents labels and speakable sentences. Raises EpubError."""
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            opf_path = _opf_path(archive, names)
            opf = _xml(archive, opf_path)
            title, manifest, spine, toc_href = _package(opf)
            base = _dir(opf_path)
            toc_path = _join(base, toc_href) if toc_href else ""
            entries = _contents(archive, names, toc_path, manifest, spine, base)
            chapters = tuple(
                _chapter(archive, names, entry, entries, index)
                for index, entry in enumerate(entries)
            )
    except (OSError, BadZipFile, ElementTree.ParseError, KeyError) as e:
        raise EpubError("Could not open this book.") from e
    if not chapters:
        raise EpubError("This book has no chapters.")
    return EpubBook(title, chapters)


@dataclass(frozen=True)
class _Entry:
    label: str
    path: str
    fragment: str


def _chapter(
    archive: ZipFile,
    names: set[str],
    entry: _Entry,
    entries: tuple[_Entry, ...],
    index: int,
) -> Chapter:
    nxt = ""
    for later in entries[index + 1 :]:
        if later.path != entry.path:
            break
        if later.fragment:
            nxt = later.fragment
            break
    text = _slice_text(
        _xml(archive, entry.path) if entry.path in names else ElementTree.Element("html"),
        entry.fragment,
        nxt,
    )
    return Chapter(entry.label, tuple(split_sentences(text.replace("\u00ad", ""))))


def _opf_path(archive: ZipFile, names: set[str]) -> str:
    if "META-INF/container.xml" not in names:
        raise EpubError("Could not open this book.")
    root = _xml(archive, "META-INF/container.xml")
    for node in root.iter():
        if _local(node.tag) == "rootfile":
            path = node.attrib.get("full-path", "")
            if path:
                return path
    raise EpubError("Could not open this book.")


def _package(root: ElementTree.Element) -> tuple[str, dict[str, str], list[str], str]:
    title = ""
    manifest: dict[str, str] = {}
    media: dict[str, str] = {}
    props: dict[str, str] = {}
    spine: list[str] = []
    toc_id = ""
    for node in root.iter():
        kind = _local(node.tag)
        if kind == "title" and not title:
            title = (node.text or "").strip()
        elif kind == "item":
            item_id = node.attrib.get("id", "")
            href = node.attrib.get("href", "")
            if item_id and href:
                manifest[item_id] = href
                media[item_id] = node.attrib.get("media-type", "")
                props[item_id] = node.attrib.get("properties", "")
        elif kind == "spine":
            toc_id = node.attrib.get("toc", "")
        elif kind == "itemref":
            ref = node.attrib.get("idref", "")
            if ref:
                spine.append(ref)
    toc_href = ""
    for item_id, href in manifest.items():
        if "nav" in props.get(item_id, "").split():
            toc_href = href
            break
    if not toc_href and toc_id:
        toc_href = manifest.get(toc_id, "")
    if not toc_href:
        for item_id, kind in media.items():
            if kind == "application/x-dtbncx+xml":
                toc_href = manifest[item_id]
                break
    return title, manifest, spine, toc_href


def _contents(
    archive: ZipFile,
    names: set[str],
    toc_path: str,
    manifest: dict[str, str],
    spine: list[str],
    opf_dir: str,
) -> tuple[_Entry, ...]:
    found: list[_Entry] = []
    if toc_path in names:
        found = _toc_entries(_xml(archive, toc_path), _dir(toc_path))
    if found:
        return tuple(found)
    entries = []
    for number, ref in enumerate(spine, start=1):
        href = manifest.get(ref, "")
        if not href:
            continue
        entries.append(_Entry(f"Section {number}", _join(opf_dir, href), ""))
    return tuple(entries)


def _toc_entries(root: ElementTree.Element, base: str) -> list[_Entry]:
    points = [node for node in root.iter() if _local(node.tag) == "navPoint"]
    if points:
        entries: list[_Entry] = []
        for point in points:
            entry = _ncx_entry(point, base)
            if entry is not None:
                entries.append(entry)
        return entries
    entries: list[_Entry] = []
    for anchor in root.iter():
        if _local(anchor.tag) != "a":
            continue
        href = anchor.attrib.get("href", "")
        label = "".join(anchor.itertext()).strip()
        label = " ".join(label.split())
        if not href or not label:
            continue
        path, fragment = _split_href(href)
        entries.append(_Entry(label, _join(base, path), fragment))
    return entries


def _ncx_entry(point: ElementTree.Element, base: str) -> _Entry | None:
    label = ""
    href = ""
    for node in point:
        kind = _local(node.tag)
        if kind == "navLabel" and not label:
            label = " ".join("".join(node.itertext()).split())
        elif kind == "content" and not href:
            href = node.attrib.get("src", "")
    if not label or not href:
        return None
    path, fragment = _split_href(href)
    return _Entry(label, _join(base, path), fragment)


def _slice_text(root: ElementTree.Element, start_id: str, end_id: str) -> str:
    elements = list(root.iter())
    start = _index(elements, start_id, 0)
    end = _index(elements, end_id, len(elements)) if end_id else len(elements)
    if end < start:
        end = len(elements)
    chosen = elements[start:end]
    chosen_ids = {id(node) for node in chosen}
    parents = {child: parent for parent in root.iter() for child in parent}
    parts: list[str] = []
    for node in chosen:
        parent = parents.get(node)
        if parent is not None and id(parent) in chosen_ids:
            continue
        parts.append(_text(node))
    return " ".join(part for part in parts if part.strip())


def _text(node: ElementTree.Element) -> str:
    if _local(node.tag) in _SKIP:
        return ""
    parts = [node.text or ""]
    for child in list(node):
        parts.append(_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _index(elements: list[ElementTree.Element], ident: str, fallback: int) -> int:
    if not ident:
        return fallback
    for index, node in enumerate(elements):
        if node.attrib.get("id") == ident:
            return index
    return fallback


def _xml(archive: ZipFile, name: str) -> ElementTree.Element:
    return ElementTree.fromstring(archive.read(name))


def _split_href(href: str) -> tuple[str, str]:
    path, _, fragment = unquote(href).partition("#")
    return path, fragment


def _dir(path: str) -> str:
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _join(base: str, href: str) -> str:
    href = unquote(href).split("#", 1)[0]
    if not href:
        return ""
    if href.startswith("/"):
        return href.lstrip("/")
    parts = [part for part in base.split("/") if part]
    for part in href.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
