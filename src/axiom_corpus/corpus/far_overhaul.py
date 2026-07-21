"""Revolutionary FAR Overhaul (RFO) draft source-first adapter.

The RFO is a draft rewrite of the Federal Acquisition Regulation published in
HTML only on acquisition.gov; it is not codified in the CFR, so the canonical
eCFR pipeline cannot supply it. This adapter ingests the normalized
source-section output produced by the ``axiom-scrapers`` RFO scraper
(``us-federal/regulations-draft/part-*/{section}.txt`` + ``{section}.meta.yaml``,
format ``axiom-source-section/v1``) and emits the four durable corpus artifacts.

Draft provisions are scoped under the ``regulation-draft`` document class with
citation paths ``us/regulation-draft/48/{part}/{section}`` — parallel to the
enacted FAR at ``us/regulation/48/{part}/{section}`` but namespaced so the draft
never collides with, or is mistaken for, codified 48 CFR Chapter 1.

Opt-in: the RFO scrape output is not part of this repository. When the supplied
scrape directory is missing or holds no RFO source-sections the extractor is a
no-op (``skipped=True``, zero provisions) rather than an error, so pipelines that
do not have the scrape on hand simply skip it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TextIO

import yaml

from axiom_corpus.corpus.artifacts import CorpusArtifactStore
from axiom_corpus.corpus.coverage import ProvisionCoverageReport, compare_provision_coverage
from axiom_corpus.corpus.models import DocumentClass, ProvisionRecord, SourceInventoryItem
from axiom_corpus.corpus.supabase import deterministic_provision_id

JURISDICTION = "us"
SOURCE_FORMAT = "axiom-source-section/v1"
RFO_DOC_TYPE = "regulation-draft"
RFO_PART_LANDING = (
    "https://www.acquisition.gov/far-overhaul/far-part-deviation-guide/far-overhaul-part-{part}"
)


@dataclass(frozen=True)
class FarOverhaulExtractReport:
    """Result from an RFO draft extraction run."""

    jurisdiction: str
    document_class: str
    version: str
    skipped: bool
    part_count: int
    section_count: int
    provisions_written: int
    inventory_path: Path | None
    provisions_path: Path | None
    coverage_path: Path | None
    coverage: ProvisionCoverageReport | None
    source_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _RfoSection:
    part: str
    section: str
    work_number: str
    heading: str
    citation_label: str
    body: str
    meta_path: Path
    text_path: Path

    @property
    def citation_path(self) -> str:
        return f"us/regulation-draft/48/{self.part}/{self.section}"

    @property
    def part_citation_path(self) -> str:
        return f"us/regulation-draft/48/{self.part}"


def _section_ordinal(section: str) -> int:
    digits = ""
    for char in section:
        if char.isdigit():
            digits += char
        else:
            break
    base = int(digits) if digits else 0
    return base * 10 + (0 if section.isdigit() else 1)


def _discover_sections(scrape_dir: Path) -> list[_RfoSection]:
    """Read RFO source-sections from an axiom-scrapers output tree.

    Returns an empty list when the directory is absent or holds no RFO
    source-sections, which the caller treats as an opt-in skip.
    """
    if not scrape_dir.exists():
        return []

    sections: list[_RfoSection] = []
    for meta_path in sorted(scrape_dir.rglob("*.meta.yaml")):
        loaded = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            continue
        if loaded.get("doc_type") != RFO_DOC_TYPE:
            continue
        work_number = str(loaded.get("work_number") or "").strip()
        if "." not in work_number:
            continue
        part, _, section = work_number.partition(".")
        text_name = str(loaded.get("text_path") or f"{work_number}.txt")
        text_path = meta_path.parent / text_name
        body = text_path.read_text(encoding="utf-8").strip() if text_path.exists() else ""
        if not body:
            continue
        sections.append(
            _RfoSection(
                part=part,
                section=section,
                work_number=work_number,
                heading=str(loaded.get("heading") or "").strip(),
                citation_label=str(loaded.get("citation") or f"FAR {work_number} (RFO)").strip(),
                body=body,
                meta_path=meta_path,
                text_path=text_path,
            )
        )
    return sections


def extract_far_overhaul(
    store: CorpusArtifactStore,
    version: str,
    scrape_dir: Path,
    *,
    expression_date: date | None = None,
    source_as_of: str | None = None,
    progress_stream: TextIO | None = None,
) -> FarOverhaulExtractReport:
    document_class = DocumentClass.REGULATION_DRAFT.value
    expression_date_text = expression_date.isoformat() if expression_date is not None else None

    sections = _discover_sections(scrape_dir)
    if not sections:
        if progress_stream is not None:
            print(
                f"far-overhaul: no RFO source-sections under {scrape_dir} — skipping",
                file=progress_stream,
                flush=True,
            )
        return FarOverhaulExtractReport(
            jurisdiction=JURISDICTION,
            document_class=document_class,
            version=version,
            skipped=True,
            part_count=0,
            section_count=0,
            provisions_written=0,
            inventory_path=None,
            provisions_path=None,
            coverage_path=None,
            coverage=None,
            source_paths=(),
        )

    inventory: list[SourceInventoryItem] = []
    records: list[ProvisionRecord] = []
    source_paths: list[Path] = []
    seen_parts: dict[str, ProvisionRecord] = {}

    for item in sorted(sections, key=lambda s: (int(s.part), _section_ordinal(s.section))):
        # Snapshot the ingested source-section (text + metadata) for provenance.
        text_dest = store.source_path(
            JURISDICTION,
            DocumentClass.REGULATION_DRAFT,
            version,
            f"far-overhaul/part-{item.part}/{item.work_number}.txt",
        )
        meta_dest = store.source_path(
            JURISDICTION,
            DocumentClass.REGULATION_DRAFT,
            version,
            f"far-overhaul/part-{item.part}/{item.work_number}.meta.yaml",
        )
        text_sha = store.write_text(text_dest, item.text_path.read_text(encoding="utf-8"))
        store.write_text(meta_dest, item.meta_path.read_text(encoding="utf-8"))
        source_relative = f"far-overhaul/part-{item.part}/{item.work_number}.txt"
        source_paths.extend((text_dest, meta_dest))

        if item.part not in seen_parts:
            part_path = item.part_citation_path
            part_record = ProvisionRecord(
                jurisdiction=JURISDICTION,
                document_class=document_class,
                citation_path=part_path,
                id=deterministic_provision_id(part_path),
                heading=f"Part {item.part}",
                body=None,
                version=version,
                source_url=RFO_PART_LANDING.format(part=item.part),
                source_format=SOURCE_FORMAT,
                source_as_of=source_as_of,
                expression_date=expression_date_text,
                level=0,
                ordinal=int(item.part),
                kind="part",
            )
            seen_parts[item.part] = part_record
            records.append(part_record)
            inventory.append(
                SourceInventoryItem(
                    citation_path=part_path,
                    source_url=RFO_PART_LANDING.format(part=item.part),
                    source_format=SOURCE_FORMAT,
                    metadata={"kind": "part", "part": item.part},
                )
            )

        records.append(
            ProvisionRecord(
                jurisdiction=JURISDICTION,
                document_class=document_class,
                citation_path=item.citation_path,
                id=deterministic_provision_id(item.citation_path),
                parent_citation_path=item.part_citation_path,
                parent_id=deterministic_provision_id(item.part_citation_path),
                heading=item.heading or None,
                citation_label=item.citation_label,
                body=item.body,
                version=version,
                source_url=RFO_PART_LANDING.format(part=item.part),
                source_path=source_relative,
                source_format=SOURCE_FORMAT,
                source_as_of=source_as_of,
                expression_date=expression_date_text,
                level=1,
                ordinal=_section_ordinal(item.section),
                kind="section",
            )
        )
        inventory.append(
            SourceInventoryItem(
                citation_path=item.citation_path,
                source_url=RFO_PART_LANDING.format(part=item.part),
                source_path=source_relative,
                source_format=SOURCE_FORMAT,
                sha256=text_sha,
                metadata={
                    "kind": "section",
                    "part": item.part,
                    "section": item.section,
                    "work_number": item.work_number,
                },
            )
        )

    inventory_path = store.inventory_path(JURISDICTION, DocumentClass.REGULATION_DRAFT, version)
    provisions_path = store.provisions_path(JURISDICTION, DocumentClass.REGULATION_DRAFT, version)
    coverage_path = store.coverage_path(JURISDICTION, DocumentClass.REGULATION_DRAFT, version)
    store.write_inventory(inventory_path, inventory)
    store.write_provisions(provisions_path, records)
    coverage = compare_provision_coverage(
        tuple(inventory),
        tuple(records),
        jurisdiction=JURISDICTION,
        document_class=document_class,
        version=version,
    )
    store.write_json(coverage_path, coverage.to_mapping())

    if progress_stream is not None:
        print(
            f"far-overhaul: {len(seen_parts)} parts, {len(sections)} sections, "
            f"{len(records)} provisions",
            file=progress_stream,
            flush=True,
        )

    return FarOverhaulExtractReport(
        jurisdiction=JURISDICTION,
        document_class=document_class,
        version=version,
        skipped=False,
        part_count=len(seen_parts),
        section_count=len(sections),
        provisions_written=len(records),
        inventory_path=inventory_path,
        provisions_path=provisions_path,
        coverage_path=coverage_path,
        coverage=coverage,
        source_paths=tuple(source_paths),
    )
