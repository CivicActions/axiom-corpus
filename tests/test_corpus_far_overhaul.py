"""Tests for the Revolutionary FAR Overhaul (RFO) draft adapter."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from axiom_corpus.corpus.artifacts import CorpusArtifactStore
from axiom_corpus.corpus.far_overhaul import extract_far_overhaul

META_TEMPLATE = """\
format: axiom-source-section/v1
jurisdiction: "us-federal"
doc_type: "regulation-draft"
authority_code: "RFO"
work_number: "{work_number}"
citation: "FAR {work_number} (RFO)"
heading: "{heading}"
generation_date: "2026-07-20"
source:
  author_id: "us-far-council"
  author_name: "Federal Acquisition Regulatory Council"
  author_url: "https://www.acquisition.gov/far-overhaul"
text_path: "{work_number}.txt"
"""


def _write_section(part_dir: Path, work_number: str, heading: str, body: str) -> None:
    part_dir.mkdir(parents=True, exist_ok=True)
    (part_dir / f"{work_number}.txt").write_text(body + "\n", encoding="utf-8")
    (part_dir / f"{work_number}.meta.yaml").write_text(
        META_TEMPLATE.format(work_number=work_number, heading=heading), encoding="utf-8"
    )


def _make_scrape_dir(root: Path) -> Path:
    part1 = root / "us-federal" / "regulations-draft" / "part-1"
    _write_section(part1, "1.000", "Scope of part", "This part describes the framework...")
    _write_section(part1, "1.101", "Framework", "(a) The System is a collection...")
    return root


def test_extract_far_overhaul_writes_four_artifacts(tmp_path):
    scrape_dir = _make_scrape_dir(tmp_path / "scrape")
    store = CorpusArtifactStore(tmp_path / "corpus")

    report = extract_far_overhaul(
        store,
        version="far-overhaul-2026-07",
        scrape_dir=scrape_dir,
        expression_date=date(2026, 7, 20),
    )

    assert report.skipped is False
    assert report.part_count == 1
    assert report.section_count == 2
    # 1 synthesized part + 2 sections
    assert report.provisions_written == 3
    assert report.coverage is not None and report.coverage.complete

    records = [
        json.loads(line)
        for line in report.provisions_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    by_path = {r["citation_path"]: r for r in records}
    assert set(by_path) == {
        "us/regulation-draft/48/1",
        "us/regulation-draft/48/1/000",
        "us/regulation-draft/48/1/101",
    }
    section = by_path["us/regulation-draft/48/1/101"]
    assert section["document_class"] == "regulation-draft"
    assert section["heading"] == "Framework"
    assert section["citation_label"] == "FAR 1.101 (RFO)"
    assert section["parent_citation_path"] == "us/regulation-draft/48/1"
    assert section["kind"] == "section"
    assert "(a) The System is a collection" in section["body"]

    # Source-sections are snapshotted for provenance.
    assert report.source_paths
    assert all(path.exists() for path in report.source_paths)


def test_extract_far_overhaul_skips_when_scrape_absent(tmp_path):
    store = CorpusArtifactStore(tmp_path / "corpus")

    report = extract_far_overhaul(
        store,
        version="far-overhaul-2026-07",
        scrape_dir=tmp_path / "does-not-exist",
    )

    assert report.skipped is True
    assert report.provisions_written == 0
    assert report.inventory_path is None
    assert report.coverage is None
    # Nothing written to the corpus tree.
    assert not (tmp_path / "corpus" / "provisions").exists()


def test_extract_far_overhaul_skips_dir_without_rfo_sections(tmp_path):
    scrape_dir = tmp_path / "scrape"
    other = scrape_dir / "us-il" / "statutes"
    other.mkdir(parents=True)
    (other / "1-1.meta.yaml").write_text(
        'format: axiom-source-section/v1\ndoc_type: "statute"\nwork_number: "1-1"\n',
        encoding="utf-8",
    )
    store = CorpusArtifactStore(tmp_path / "corpus")

    report = extract_far_overhaul(store, version="v1", scrape_dir=scrape_dir)

    assert report.skipped is True
    assert report.provisions_written == 0
