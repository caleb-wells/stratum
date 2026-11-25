"""Turn source PDFs into section-aware chunks.

Chunks follow each document's own numbered headings so a chunk is a coherent
requirement, and every chunk carries jurisdiction + section path + page range.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import yaml

TARGET_CHARS = 1800   # ~450 tokens
MAX_CHARS = 2600
MIN_CHARS = 250

DEFAULT_HEADING = r"^(\d{1,2}(?:\.\d{1,2}){1,3})\.?\s+([A-Z][^\n]{2,100})$"


@dataclass
class Section:
    path: list[str]
    part: str | None
    page_start: int
    page_end: int
    lines: list[str] = field(default_factory=list)


@dataclass
class Chunk:
    id: str
    doc_id: str
    jurisdiction: str
    jurisdiction_name: str
    layer: str
    doc_title: str
    doc_date: str | None
    url: str
    section_path: str
    page_start: int
    page_end: int
    text: str

    @property
    def header(self) -> str:
        date = f" (dated {self.doc_date})" if self.doc_date else ""
        return f"[{self.jurisdiction_name}] {self.doc_title}{date} > {self.section_path}"

    @property
    def embed_text(self) -> str:
        return f"{self.header}\n\n{self.text}"


def load_manifest(root: Path) -> dict:
    return yaml.safe_load((root / "corpus" / "sources.yaml").read_text())


def _table_rows(page, table) -> list[str]:
    """Render a table as self-describing rows ("Header: value; Header: value").

    Cells are read one at a time from their own rectangle, which keeps numbers
    intact and aligned with the right row; plain text extraction scrambles them.
    """
    grid = []
    for row in table.rows:
        cells = []
        for cell in row.cells:
            text = page.get_text("text", clip=pymupdf.Rect(cell) + (-1, -1, 1, 1)) if cell else ""
            cells.append(re.sub(r"\s+", " ", text).strip())
        grid.append(cells)
    header, body = grid[0], grid[1:]
    lines = ["Table:", ""]
    for cells in body:
        pairs = [f"{h}: {c}" if h else c for h, c in zip(header, cells) if c]
        if pairs:
            lines += ["- " + "; ".join(pairs), ""]
    return lines


def _page_lines(page, columns: int = 1) -> list[str]:
    tables = [t for t in page.find_tables().tables if t.row_count >= 3 and t.col_count >= 2]
    boxes = [pymupdf.Rect(t.bbox) for t in tables]
    width, height = page.rect.width, page.rect.height

    def order(x0: float, y0: float, x1: float) -> tuple:
        if columns == 1:
            return (0, y0, x0)
        if x1 - x0 > 0.6 * width:                    # spans both columns
            return (-1, y0, x0) if y0 < 0.25 * height else (2, y0, x0)
        return (0 if x0 < 0.45 * width else 1, y0, x0)

    items: list[tuple[tuple, list[str]]] = []
    for b in page.get_text("blocks"):
        if b[6] != 0:                                # image block
            continue
        centre = pymupdf.Point((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        if any(centre in box for box in boxes):       # table text is rendered separately
            continue
        text = b[4].replace("\u00ad\n", "").replace("\u00ad", "")
        items.append((order(b[0], b[1], b[2]), text.splitlines() + [""]))
    for t, box in zip(tables, boxes):
        items.append((order(box.x0, box.y0, box.x1), [""] + _table_rows(page, t)))
    lines: list[str] = []
    for _, chunk in sorted(items, key=lambda i: i[0]):
        lines.extend(chunk)
    return lines


def _depth(num: str) -> int:
    parts = [p for p in num.split(".") if p != ""]
    if len(parts) == 2 and parts[1] == "0":
        return 1
    return len(parts)


def _key(num: str) -> tuple[int, ...]:
    k = [int(p) for p in num.split(".") if p != ""]
    return tuple(k[:1]) if len(k) == 2 and k[1] == 0 else tuple(k)


def _in_order(num: str, last: str | None) -> bool:
    """Reject table values like '1.3 Mitigation' that only look like headings."""
    new = _key(num)
    if last is None:
        return new[0] <= 2
    old = _key(last)
    if new <= old:
        return False
    if new[0] == old[0]:
        n = min(len(new), len(old))
        diff = next((i for i in range(n) if new[i] != old[i]), None)
        return diff is None or new[diff] - old[diff] <= 2
    return new[0] - old[0] <= 2 and all(x <= 2 for x in new[1:])


def _clean(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


_REFS = re.compile(r"^(APPENDIX [A-Z] [-\u2013] )?REFERENCES( AND SOURCES)?\.?$")
_NUM_ONLY = re.compile(r"^\d{1,2}(?:\.\d{1,2}){1,3}\.?$")


def _join_split_headings(lines: list[str]) -> list[str]:
    """Some PDFs put the section number and its title on separate lines."""
    out, i = [], 0
    while i < len(lines):
        l = lines[i]
        if _NUM_ONLY.match(l):
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            if j < len(lines) and j - i <= 2 and lines[j][:1].isupper() and len(lines[j]) <= 100:
                out.append(f"{l} {lines[j]}")
                i = j + 1
                continue
        out.append(l)
        i += 1
    return out


def parse_numbered(pdf: Path, doc: dict) -> list[Section]:
    strip = [re.compile(p) for p in doc.get("strip", [])]
    heading = re.compile(doc.get("heading_regex", DEFAULT_HEADING))
    custom = "heading_regex" in doc
    part_re = re.compile(doc["part_from_header"]) if doc.get("part_from_header") else None
    part_titles = doc.get("part_titles", {})

    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    part: str | None = None
    current: Section | None = None
    last_num: str | None = None
    skipping = False
    skip = [k.upper() for k in doc.get("skip_sections", ["REFERENCES"])]

    for pno, page in enumerate(pymupdf.open(pdf), start=1):
        raw = _join_split_headings([_clean(l) for l in _page_lines(page)])
        if sum("....." in l for l in raw) >= 4:      # table-of-contents page
            continue
        if part_re:
            for l in raw[:6]:
                m = part_re.match(l)
                if m:
                    new_part = part_titles.get(m.group(1), l)
                    if new_part != part:
                        part, stack, current = new_part, [], None
                        last_num, skipping = None, False
                    break
        for l in raw:
            if any(s.match(l) for s in strip) or "....." in l:
                continue
            m = heading.match(l) if l else None
            if m and not l.rstrip().endswith(",") and (custom or _in_order(m.group(1), last_num)):
                num, title = m.group(1), m.group(2).strip()
                rest = ""
                if ". " in title:                      # "2.2 Method. Body text continues..."
                    title, rest = title.split(". ", 1)
                label = f"{num} {title}".rstrip(".")
                if custom:
                    stack = [(1, label)]
                else:
                    last_num = num
                    d = _depth(num)
                    stack = [s for s in stack if s[0] < d] + [(d, label)]
                skipping = any(k in title.upper() for k in skip)
                current = Section([s[1] for s in stack], part, pno, pno)
                if not skipping:
                    sections.append(current)
                    if rest:
                        current.lines.append(rest)
                continue
            if _REFS.match(l):
                skipping = True
                continue
            if current is None:
                current = Section(["Preamble"], part, pno, pno)
                sections.append(current)
            if skipping:
                continue
            current.lines.append(l)
            if l:
                current.page_end = pno
    return sections


def parse_toc(pdf: Path, doc: dict) -> list[Section]:
    strip = [re.compile(p) for p in doc.get("strip", [])]
    d = pymupdf.open(pdf)
    first = doc.get("first_page", 1)
    skip = set(doc.get("skip_sections", []))
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    by_page: dict[int, list[tuple[int, str]]] = {}
    for lvl, title, page in d.get_toc():
        by_page.setdefault(page, []).append((lvl, _clean(title)))

    sections: list[Section] = []
    chapter = ""
    stack: list[tuple[int, str]] = []
    current: Section | None = None
    skipping = False
    for pno in range(first, len(d) + 1):
        pending = list(by_page.get(pno, []))
        raw = [_clean(l) for l in _page_lines(d[pno - 1], doc.get("columns", 1))]
        i = 0
        while i < len(raw):
            l = raw[i]
            if any(s.match(l) for s in strip):
                i += 1
                continue
            matched = None
            for cand in pending:
                target = norm(cand[1])
                joined = ""
                for span in range(1, 5):          # headings may wrap over several lines
                    joined = norm(" ".join(raw[i:i + span]))
                    if joined == target:
                        matched = (cand, span)
                        break
                    if not target.startswith(joined):
                        break
                if matched:
                    break
            if matched:
                (lvl, title), span = matched
                pending.remove(matched[0])
                i += span
                if title.upper().startswith(("CHAPTER", "APPENDIX")):
                    chapter, stack = title.title(), []
                    skipping = False
                    current = Section([chapter], None, pno, pno)
                    sections.append(current)
                    continue
                skipping = title.upper() in skip
                stack = [s for s in stack if s[0] < lvl] + [(lvl, title.title() if title.isupper() else title)]
                current = Section(([chapter] if chapter else []) + [s[1] for s in stack], None, pno, pno)
                if not skipping:
                    sections.append(current)
                continue
            if current is not None and not skipping:
                current.lines.append(l)
                if l:
                    current.page_end = pno
            i += 1
    return sections


def _paragraphs(lines: list[str]) -> list[str]:
    paras, buf = [], []
    for l in lines:
        if l:
            buf.append(l)
        elif buf:
            paras.append(" ".join(buf))
            buf = []
    if buf:
        paras.append(" ".join(buf))
    return paras


def _split(text_paras: list[str]) -> list[str]:
    out, buf = [], ""
    for p in text_paras:
        while len(p) > MAX_CHARS:                  # very long paragraph: cut at sentence ends
            cut = p.rfind(". ", 0, TARGET_CHARS) + 1 or TARGET_CHARS
            if buf:
                out.append(buf); buf = ""
            out.append(p[:cut].strip()); p = p[cut:].strip()
        if buf and len(buf) + len(p) > TARGET_CHARS:
            out.append(buf); buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    if len(out) > 1 and len(out[-1]) < MIN_CHARS:
        tail = out.pop()
        out[-1] = out[-1] + "\n\n" + tail
    return out


def build_chunks(root: Path) -> list[Chunk]:
    manifest = load_manifest(root)
    chunks: list[Chunk] = []
    for doc in manifest["documents"]:
        pdf = root / "corpus" / "raw" / doc["file"]
        j = manifest["jurisdictions"][doc["jurisdiction"]]
        sections = (parse_toc if doc["parser"] == "toc" else parse_numbered)(pdf, doc)
        for s in sections:
            paras = _paragraphs(s.lines)
            body = "\n\n".join(paras)
            if len(body) < 40:      # heading with no body: its children carry the content
                continue
            path = " > ".join(([s.part] if s.part else []) + s.path)
            for n, piece in enumerate(_split(paras)):
                cid = hashlib.sha1(f"{doc['id']}|{path}|{n}|{piece[:80]}".encode()).hexdigest()[:16]
                chunks.append(Chunk(
                    id=cid, doc_id=doc["id"], jurisdiction=doc["jurisdiction"],
                    jurisdiction_name=j["name"], layer=j["layer"], doc_title=doc["title"],
                    doc_date=doc.get("doc_date"), url=doc["url"], section_path=path,
                    page_start=s.page_start, page_end=s.page_end, text=piece,
                ))
    return chunks


if __name__ == "__main__":
    import collections, sys
    root = Path(__file__).resolve().parents[4]
    cs = build_chunks(root)
    by = collections.Counter(c.doc_id for c in cs)
    for k, v in by.items():
        sizes = [len(c.text) for c in cs if c.doc_id == k]
        print(f"{k}: {v} chunks, avg {sum(sizes)//len(sizes)} chars, max {max(sizes)}")
    if len(sys.argv) > 1:
        for c in cs:
            if c.doc_id == sys.argv[1]:
                print(f"p{c.page_start}-{c.page_end} | {c.section_path} | {len(c.text)}")
