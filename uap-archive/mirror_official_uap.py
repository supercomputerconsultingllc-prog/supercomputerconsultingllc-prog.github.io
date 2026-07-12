#!/usr/bin/env python3
"""Build a public, provenance-preserving mirror of current official U.S. UAP releases.

Immediate mirror scope:
  * All eight Department of War PURSUE bulk archives, Releases 01–04.
  * All four NARA Record Group 615 electronic-record ZIPs and metadata JSON files.

The script also inventories every downloadable ZIP and metadata JSON linked from NARA's
UAP bulk-download page. Large files are split into deterministic numbered parts only
because GitHub Release assets have a per-file size limit. Original SHA-256 hashes are
recorded so the exact government ZIP can be reconstructed and verified.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

REPOSITORY = os.environ.get("GH_REPOSITORY", "")
RELEASE_TAG = os.environ.get("RELEASE_TAG", "official-uap-archive-2026-07-11")
CHUNK_SIZE = 1_750_000_000
NARA_PAGE = "https://www.archives.gov/research/catalog/catalog-bulk-downloads/uap-bulk-download"
PURSUE_PAGE = "https://www.war.gov/ufo/"
USER_AGENT = "Official-UAP-Public-Archive/1.0 (+public-interest preservation)"


@dataclass(frozen=True)
class SourceFile:
    collection: str
    release: str
    release_date: str
    agency: str
    category: str
    official_url: str
    published_size: str
    local_name: str
    kind: str = "zip"


SOURCES: list[SourceFile] = [
    SourceFile("PURSUE", "01", "2026-05-08", "U.S. Department of War", "documents_and_other_media", "https://www.war.gov/medialink/ufo/bundle/Release_1.zip", "1.2 GB", "pursue_release_01_documents__Release_1.zip"),
    SourceFile("PURSUE", "01", "2026-05-08", "U.S. Department of War", "videos", "https://d34w7g4gy10iej.cloudfront.net/uapvideos.zip", "1.3 GB", "pursue_release_01_videos__uapvideos.zip"),
    SourceFile("PURSUE", "02", "2026-05-22", "U.S. Department of War", "documents_and_other_media", "https://www.war.gov/medialink/ufo/052226/release_02/release_02_document_bundle.zip", "70.1 MB", "pursue_release_02_documents__release_02_document_bundle.zip"),
    SourceFile("PURSUE", "02", "2026-05-22", "U.S. Department of War", "videos", "https://d34w7g4gy10iej.cloudfront.net/uap052226.zip", "5.6 GB", "pursue_release_02_videos__uap052226.zip"),
    SourceFile("PURSUE", "03", "2026-06-12", "U.S. Department of War", "documents_and_other_media", "https://www.war.gov/medialink/ufo/061226/release_03/release_03_documents.zip", "826 MB", "pursue_release_03_documents__release_03_documents.zip"),
    SourceFile("PURSUE", "03", "2026-06-12", "U.S. Department of War", "videos", "https://d34w7g4gy10iej.cloudfront.net/release_03/uap_videos_061226.zip", "4.6 GB", "pursue_release_03_videos__uap_videos_061226.zip"),
    SourceFile("PURSUE", "04", "2026-07-10", "U.S. Department of War", "documents_and_other_media", "https://www.war.gov/medialink/ufo/071026/release_04/release_04_documents_071026.zip", "227 MB", "pursue_release_04_documents__release_04_documents_071026.zip"),
    SourceFile("PURSUE", "04", "2026-07-10", "U.S. Department of War", "videos", "https://d34w7g4gy10iej.cloudfront.net/release_04/uap_release04_videos_071026.zip", "1.4 GB", "pursue_release_04_videos__uap_release04_videos_071026.zip"),
    SourceFile("NARA_RG615", "NRC", "2025-04-24", "U.S. Nuclear Regulatory Commission / NARA", "electronic_records", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/zips/electronic-records/488808322.zip", "11.74 MB", "nara_rg615_488808322.zip"),
    SourceFile("NARA_RG615", "FAA", "2025-04-24", "Federal Aviation Administration / NARA", "electronic_records", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/zips/electronic-records/493468575.zip", "63.10 MB", "nara_rg615_493468575.zip"),
    SourceFile("NARA_RG615", "ODNI", "2025-04-24", "Office of the Director of National Intelligence / NARA", "electronic_records", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/zips/electronic-records/493468579.zip", "1.29 MB", "nara_rg615_493468579.zip"),
    SourceFile("NARA_RG615", "OSD", "2025-04-24", "Office of the Secretary of Defense / NARA", "electronic_records", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/zips/electronic-records/493468580.zip", "103.37 MB", "nara_rg615_493468580.zip"),
    SourceFile("NARA_RG615", "NRC", "2025-04-24", "U.S. Nuclear Regulatory Commission / NARA", "catalog_metadata", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/JSON/catalog-export-488808322.json", "metadata", "nara_rg615_catalog-export-488808322.json", "json"),
    SourceFile("NARA_RG615", "FAA", "2025-04-24", "Federal Aviation Administration / NARA", "catalog_metadata", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/JSON/catalog-export-493468575.json", "metadata", "nara_rg615_catalog-export-493468575.json", "json"),
    SourceFile("NARA_RG615", "ODNI", "2025-04-24", "Office of the Director of National Intelligence / NARA", "catalog_metadata", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/JSON/catalog-export-493468579.json", "metadata", "nara_rg615_catalog-export-493468579.json", "json"),
    SourceFile("NARA_RG615", "OSD", "2025-04-24", "Office of the Secretary of Defense / NARA", "catalog_metadata", "https://s3.amazonaws.com/NARAprodstorage/lz/bulk-downloads/uaps/JSON/catalog-export-493468580.json", "metadata", "nara_rg615_catalog-export-493468580.json", "json"),
]


def run(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, check=check, text=True, capture_output=capture)


def sha256_file(path: Path, block_size: int = 16 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(block_size):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def download_source(source: SourceFile, target: Path) -> None:
    run([
        "curl", "--location", "--fail", "--show-error", "--silent",
        "--retry", "12", "--retry-delay", "5", "--retry-all-errors",
        "--connect-timeout", "30", "--speed-time", "180", "--speed-limit", "1024",
        "--user-agent", USER_AGENT,
        "--output", str(target), source.official_url,
    ])
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"No data downloaded from {source.official_url}")


def ensure_release() -> None:
    if not REPOSITORY:
        raise RuntimeError("GH_REPOSITORY is not set")
    existing = run(["gh", "release", "view", RELEASE_TAG, "--repo", REPOSITORY], check=False, capture=True)
    if existing.returncode == 0:
        return
    notes = (
        "Public-interest preservation mirror of official U.S. government UAP records. "
        "Includes all Department of War PURSUE Releases 01–04 through July 10, 2026, "
        "plus all four electronic-record series presently listed in NARA Record Group 615.\n\n"
        "Every mirrored source has provenance, byte count, SHA-256, and ZIP-entry metadata. "
        "Files larger than GitHub's per-asset limit are split into numbered parts without "
        "recompression or modification. Reconstruct with `cat name.zip.part* > name.zip`, "
        "then verify against `OFFICIAL_UAP_SHA256SUMS.txt`.\n\n"
        "The release also includes a machine-readable inventory of every ZIP and JSON linked "
        "from NARA's historical UAP bulk-download page."
    )
    run([
        "gh", "release", "create", RELEASE_TAG, "--repo", REPOSITORY,
        "--title", "Official U.S. UAP Archive — PURSUE 01–04 + NARA RG 615",
        "--notes", notes,
    ])


def existing_assets() -> set[str]:
    result = run([
        "gh", "release", "view", RELEASE_TAG, "--repo", REPOSITORY,
        "--json", "assets", "--jq", ".assets[].name",
    ], capture=True)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def upload(path: Path) -> None:
    for attempt in range(1, 6):
        result = run([
            "gh", "release", "upload", RELEASE_TAG, str(path),
            "--repo", REPOSITORY, "--clobber",
        ], check=False)
        if result.returncode == 0:
            return
        if attempt == 5:
            raise RuntimeError(f"Failed to upload {path.name} after five attempts")
        time.sleep(attempt * 20)


def inspect_zip(source: SourceFile, archive: Path) -> tuple[list[dict[str, Any]], str]:
    members: list[dict[str, Any]] = []
    status = "ok"
    try:
        with zipfile.ZipFile(archive) as zf:
            bad = zf.testzip()
            if bad:
                status = f"CRC failure at {bad}"
            for info in zf.infolist():
                members.append({
                    "collection": source.collection,
                    "release": source.release,
                    "release_date": source.release_date,
                    "category": source.category,
                    "source_archive": source.local_name,
                    "member_path": info.filename,
                    "is_directory": info.is_dir(),
                    "compressed_bytes": info.compress_size,
                    "uncompressed_bytes": info.file_size,
                    "crc32": f"{info.CRC:08x}",
                    "compression_method": info.compress_type,
                    "member_modified": "%04d-%02d-%02dT%02d:%02d:%02d" % info.date_time,
                })
    except Exception as exc:
        status = f"ZIP inspection error: {type(exc).__name__}: {exc}"
    return members, status


def upload_source_file(path: Path) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if path.stat().st_size <= CHUNK_SIZE:
        upload(path)
        parts.append({"asset": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
        path.unlink()
        return parts

    with path.open("rb") as src:
        number = 1
        while True:
            part_name = f"{path.name}.part{number:03d}"
            part_path = path.parent / part_name
            digest = hashlib.sha256()
            written = 0
            with part_path.open("wb") as out:
                while written < CHUNK_SIZE:
                    chunk = src.read(min(16 * 1024 * 1024, CHUNK_SIZE - written))
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            if written == 0:
                part_path.unlink(missing_ok=True)
                break
            upload(part_path)
            parts.append({"asset": part_name, "bytes": written, "sha256": digest.hexdigest()})
            part_path.unlink()
            number += 1
    path.unlink()
    return parts


def load_completed(source: SourceFile, workdir: Path, assets: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    metadata_name = f"{source.local_name}.mirror-metadata.json"
    contents_name = f"{source.local_name}.contents.json"
    if metadata_name not in assets:
        return None
    for name in (metadata_name, contents_name):
        if name == contents_name and source.kind != "zip":
            continue
        result = run([
            "gh", "release", "download", RELEASE_TAG, "--repo", REPOSITORY,
            "--pattern", name, "--dir", str(workdir), "--clobber",
        ], check=False)
        if result.returncode != 0:
            return None
    try:
        metadata = json.loads((workdir / metadata_name).read_text(encoding="utf-8"))
        required_assets = {part["asset"] for part in metadata.get("parts", [])}
        if not required_assets or not required_assets.issubset(assets):
            return None
        members: list[dict[str, Any]] = []
        if source.kind == "zip":
            members = json.loads((workdir / contents_name).read_text(encoding="utf-8"))
        print(f"Already complete; skipping {source.local_name}", flush=True)
        return metadata, members
    except Exception:
        return None


def build_nara_inventory(workdir: Path) -> tuple[Path, Path, Path, Path]:
    html = fetch_bytes(NARA_PAGE).decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, str]] = []
    section = ""
    for node in soup.find_all(["h2", "h3", "h4", "a"]):
        if node.name in {"h2", "h3", "h4"}:
            text = " ".join(node.get_text(" ", strip=True).split())
            if text:
                section = text
            continue
        href = node.get("href", "")
        if not href:
            continue
        if not (re.search(r"catalog-export-\d+\.json(?:\?|$)", href) or re.search(r"(?:\d+|\d+-images-\d+|\d+-pdfs-\d+)\.zip(?:\?|$)", href)):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.archives.gov" + href
        elif not href.startswith("http"):
            href = urllib.parse.urljoin(NARA_PAGE, href)
        filename = href.split("?")[0].rstrip("/").split("/")[-1]
        kind = "metadata_json" if filename.endswith(".json") else "bulk_zip"
        parent = node.parent
        context = " ".join(parent.get_text(" ", strip=True).split()) if parent else node.get_text(" ", strip=True)
        rows.append({
            "section": section,
            "kind": kind,
            "filename": filename,
            "official_url": href,
            "context": context[:1000],
        })
    dedup: dict[str, dict[str, str]] = {row["official_url"]: row for row in rows}
    rows = sorted(dedup.values(), key=lambda r: (r["section"], r["kind"], r["filename"]))

    json_path = workdir / "NARA_UAP_ALL_DOWNLOADS_INVENTORY.json"
    json_path.write_text(json.dumps({
        "source_page": NARA_PAGE,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "download_count": len(rows),
        "downloads": rows,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    csv_path = workdir / "NARA_UAP_ALL_DOWNLOADS_INVENTORY.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["section", "kind", "filename", "official_url", "context"])
        writer.writeheader()
        writer.writerows(rows)

    sh_path = workdir / "download_all_nara_uap.sh"
    sh_lines = [
        "#!/usr/bin/env bash", "set -euo pipefail", 'DEST="${1:-nara-uap-downloads}"',
        'mkdir -p "$DEST"', 'cd "$DEST"',
    ]
    for row in rows:
        sh_lines.append(f"curl -L --fail --retry 12 --retry-all-errors -C - -o {json.dumps(row['filename'])} {json.dumps(row['official_url'])}")
    sh_path.write_text("\n".join(sh_lines) + "\n", encoding="utf-8")

    ps_path = workdir / "download_all_nara_uap.ps1"
    ps_lines = [
        "param([string]$Destination = 'nara-uap-downloads')",
        "$ErrorActionPreference = 'Stop'",
        "New-Item -ItemType Directory -Force -Path $Destination | Out-Null",
    ]
    for row in rows:
        safe_url = row["official_url"].replace("'", "''")
        safe_name = row["filename"].replace("'", "''")
        ps_lines.append(f"& curl.exe -L --fail --retry 12 --retry-all-errors -C - -o (Join-Path $Destination '{safe_name}') '{safe_url}'")
    ps_path.write_text("\n".join(ps_lines) + "\n", encoding="utf-8")
    return json_path, csv_path, sh_path, ps_path


def build_aggregate(workdir: Path, records: list[dict[str, Any]], members: list[dict[str, Any]]) -> list[Path]:
    manifest_path = workdir / "OFFICIAL_UAP_MIRROR_MANIFEST.json"
    manifest_path.write_text(json.dumps({
        "title": "Official U.S. UAP Archive",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "release_tag": RELEASE_TAG,
        "repository": REPOSITORY,
        "official_sources": [PURSUE_PAGE, NARA_PAGE],
        "immediate_mirror_scope": "PURSUE Releases 01–04 and NARA RG 615 electronic-record series",
        "sources": records,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    members_path = workdir / "OFFICIAL_UAP_ZIP_CONTENTS.csv"
    fields = ["collection", "release", "release_date", "category", "source_archive", "member_path", "is_directory", "compressed_bytes", "uncompressed_bytes", "crc32", "compression_method", "member_modified"]
    with members_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(members)

    sums_path = workdir / "OFFICIAL_UAP_SHA256SUMS.txt"
    with sums_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(f"{record['sha256']}  {record['local_name']}\n")
            for part in record.get("parts", []):
                fh.write(f"{part['sha256']}  {part['asset']}\n")

    readme_path = workdir / "OFFICIAL_UAP_ARCHIVE_README.txt"
    readme_path.write_text(
        "OFFICIAL U.S. UAP ARCHIVE\n"
        "=========================\n\n"
        "Mirrored collections:\n"
        "  1. Department of War PURSUE Releases 01–04 through July 10, 2026.\n"
        "  2. NARA Record Group 615 electronic-record ZIPs and catalog metadata.\n\n"
        "Provenance pages:\n"
        f"  {PURSUE_PAGE}\n  {NARA_PAGE}\n\n"
        "Large original ZIPs are stored as .part001, .part002, etc. Reconstruct on Linux/macOS:\n"
        "  cat filename.zip.part* > filename.zip\n\n"
        "On Windows CMD:\n"
        "  copy /b filename.zip.part001+filename.zip.part002+filename.zip.part003 filename.zip\n\n"
        "Verify reconstructed and unsplit originals against OFFICIAL_UAP_SHA256SUMS.txt.\n"
        "NARA_UAP_ALL_DOWNLOADS_INVENTORY.* catalogs every downloadable object on NARA's page.\n",
        encoding="utf-8",
    )
    return [manifest_path, members_path, sums_path, readme_path]


def main() -> int:
    ensure_release()
    assets = existing_assets()
    records: list[dict[str, Any]] = []
    all_members: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="official-uap-") as tmp:
        workdir = Path(tmp)
        for index, source in enumerate(SOURCES, start=1):
            print(f"\n[{index}/{len(SOURCES)}] {source.collection} {source.release} {source.category}", flush=True)
            completed = load_completed(source, workdir, assets)
            if completed:
                record, members = completed
                records.append(record)
                all_members.extend(members)
                continue

            path = workdir / source.local_name
            started = time.time()
            download_source(source, path)
            original_hash = sha256_file(path)
            members: list[dict[str, Any]] = []
            validation = "not_applicable"
            if source.kind == "zip":
                members, validation = inspect_zip(source, path)
                all_members.extend(members)
            elif source.kind == "json":
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                    validation = "valid_json"
                except Exception as exc:
                    validation = f"JSON validation error: {type(exc).__name__}: {exc}"

            record: dict[str, Any] = {
                **asdict(source),
                "downloaded_bytes": path.stat().st_size,
                "sha256": original_hash,
                "validation": validation,
                "zip_entry_count": len(members),
                "mirrored_at_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.time() - started, 2),
            }
            record["parts"] = upload_source_file(path)

            if source.kind == "zip":
                contents_path = workdir / f"{source.local_name}.contents.json"
                contents_path.write_text(json.dumps(members, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                upload(contents_path)
                contents_path.unlink()
                assets.add(contents_path.name)

            metadata_path = workdir / f"{source.local_name}.mirror-metadata.json"
            metadata_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            upload(metadata_path)
            metadata_path.unlink()
            records.append(record)
            assets.update(part["asset"] for part in record["parts"])
            assets.add(metadata_path.name)

        inventory_files = build_nara_inventory(workdir)
        aggregate_files = build_aggregate(workdir, records, all_members)
        for artifact in [*inventory_files, *aggregate_files]:
            upload(artifact)

    print("\nOfficial UAP mirror completed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
