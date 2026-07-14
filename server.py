from __future__ import annotations

import argparse
import base64
import csv
from email import policy
from email.parser import BytesParser
import hashlib
import html
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import ssl
ssl._create_default_https_context = ssl._create_unverified_context


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "target_file": "221_targets_literature_search_enriched.csv",
    "fallback_target_file": "221.csv",
    "hotspots_file": "test_hotspots.json",
    "output_file": "outputs/hotspots.json",
    "literature_corpus_file": "outputs/literature_corpus.md",
    "paper_records_file": "outputs/paper_records.json",
    "science_records_file": "outputs/science_records.json",
    "pdf_dir": "outputs/pdfs",
    "markdown_dir": "outputs/markdown",
    "parse_tasks_dir": "outputs/parse_tasks",
    "storage_base_dir": "",
    "max_pdf_bytes": 80 * 1024 * 1024,
    "mineru_adapter": {
        "mode": "command",
        "command": "mineru",
        "args": ["-p", "{pdf_path}", "-o", "{output_dir}"],
        "timeout_seconds": 7200,
        "service_url": "",
        "api_base": "https://mineru.net",
        "api_model_version": "vlm",
        "api_language": "en",
        "api_enable_formula": True,
        "api_enable_table": True,
        "api_poll_interval_seconds": 5,
    },
    "mineru_models": {
        "auto_prepare": True,
        "source": "modelscope",
        "model_type": "pipeline",
        "timeout_seconds": 7200,
    },
    "cache_dir": "data/cache",
    "max_targets_per_run": 12,
    "max_papers_per_target": 5,
    "max_topic_papers": 200,
    "arxiv_page_size": 50,
    "topic_relevance_threshold": 0.18,
    "recent_years": 2,
    "default_timeframe_months": 12,
    "target_search_neighbor_radius_deg": 2.0,
    "target_search_max_expanded_targets": 4,
    "target_search_max_related_targets": 5,
    "target_search_alias_limit": 10,
    "target_search_strict_candidates": True,
    "target_search_rank_weights": {
        "semantic": 0.35,
        "target": 0.30,
        "evidence": 0.20,
        "recency": 0.15,
    },
    "arxiv_enabled": True,
    "ads_enabled": True,
    "deepseek_enabled": True,
    "deepseek_model": "deepseek-chat",
    "deepseek_paper_summary_enabled": False,
    "paper_summary_batch_size": 8,
    "paper_summary_abstract_chars": 1800,
    "science_markdown_max_chars": 120000,
    "overall_analysis_abstract_chars": 900,
    "overall_analysis_max_papers": 220,
}


@dataclass
class Target:
    id: str
    name: str | None = None
    aliases: list[str] = field(default_factory=list)
    ra_deg: str | None = None
    dec_deg: str | None = None
    vmag: str | None = None
    teff_k: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Paper:
    title: str
    authors: list[str]
    abstract: str
    published_at: str
    year: int | None
    url: str
    source: str
    target_id: str
    relevance: float = 0.0


@dataclass
class PaperRecord:
    paper_id: str
    title: str
    arxiv_id: str | None = None
    doi: str | None = None
    source_url: str | None = None
    version: str | None = None
    download_time: str | None = None
    fetch_status: str = "not_fetched"
    failure_reason: str | None = None
    candidate_links: list[str] = field(default_factory=list)
    pdf_path: str | None = None
    markdown_path: str | None = None
    parse_status: str = "not_started"
    parse_error: str | None = None
    parse_time: str | None = None
    parser: str | None = None


@dataclass
class ParseTask:
    task_id: str
    paper_id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str | None = None
    completed_at: str | None = None
    message: str | None = None
    markdown_path: str | None = None
    parse_status: str = "not_started"
    progress_percent: float | None = None
    progress_stage: str | None = None
    worker_pid: int | None = None
    record: dict[str, Any] | None = None


@dataclass
class EvidenceRef:
    paper_id: str
    section: str
    quote: str
    page: str | None = None


SCIENCE_EXTRACTOR_VERSION = "n4-cn-analysis-v1"


@dataclass
class ScienceRecord:
    paper_id: str
    source: str = "abstract"
    extractor_version: str = SCIENCE_EXTRACTOR_VERSION
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    targets: list[dict[str, Any]] = field(default_factory=list)
    methods: list[dict[str, Any]] = field(default_factory=list)
    parameters: list[dict[str, Any]] = field(default_factory=list)
    conclusions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)


@dataclass
class Analysis:
    summary: str
    topics: list[str]
    conclusion: str
    significance: str
    related_targets: list[str]
    relevance_score: float
    provider: str


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config_file = ROOT / "config.json"
    if config_file.exists():
        config.update(json.loads(config_file.read_text(encoding="utf-8")))
    return config


CONFIG = load_config()


def deep_merge_dict(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_config_values(values: dict[str, Any]) -> None:
    config_file = ROOT / "config.json"
    existing: dict[str, Any] = {}
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    updated = deep_merge_dict(existing, values)
    config_file.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    CONFIG.update(deep_merge_dict(CONFIG, values))


def load_env_file() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        key, raw = value.split("=", 1)
        key = key.strip()
        raw = raw.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = raw


load_env_file()


def write_env_values(values: dict[str, str]) -> None:
    env_file = ROOT / ".env"
    existing: dict[str, str] = {}
    order: list[str] = []
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.strip().startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            key = key.strip()
            existing[key] = raw.strip()
            order.append(key)
    for key, value in values.items():
        if value is None:
            continue
        if key not in order:
            order.append(key)
        existing[key] = str(value).strip()
        if str(value).strip():
            os.environ[key] = str(value).strip()
        elif key in os.environ:
            del os.environ[key]
    lines = [f"{key}={existing.get(key, '')}" for key in order if key]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def configured_storage_base() -> Path | None:
    raw = clean_text(str(CONFIG.get("storage_base_dir") or ""))
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def config_path(key: str, default: str) -> Path:
    raw = clean_text(str(CONFIG.get(key) or default))
    path = Path(raw)
    if path.is_absolute():
        return path
    base = configured_storage_base()
    if base:
        parts = path.parts
        if parts and parts[0].lower() == "outputs":
            path = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        return base / path
    return ROOT / path


def config_path_display(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def stored_file_path(value: str | Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return ROOT / path


def is_path_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def ensure_dirs() -> None:
    (ROOT / CONFIG["cache_dir"]).mkdir(parents=True, exist_ok=True)
    (ROOT / "outputs").mkdir(parents=True, exist_ok=True)
    config_path("output_file", "outputs/hotspots.json").parent.mkdir(parents=True, exist_ok=True)
    config_path("literature_corpus_file", "outputs/literature_corpus.md").parent.mkdir(parents=True, exist_ok=True)
    config_path("paper_records_file", "outputs/paper_records.json").parent.mkdir(parents=True, exist_ok=True)
    config_path("science_records_file", "outputs/science_records.json").parent.mkdir(parents=True, exist_ok=True)
    config_path("pdf_dir", "outputs/pdfs").mkdir(parents=True, exist_ok=True)
    config_path("markdown_dir", "outputs/markdown").mkdir(parents=True, exist_ok=True)
    config_path("parse_tasks_dir", "outputs/parse_tasks").mkdir(parents=True, exist_ok=True)
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)


def log_event(message: str, data: dict[str, Any] | None = None) -> None:
    ensure_dirs()
    payload = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "message": message,
        "data": data or {},
    }
    try:
        with (ROOT / "logs" / "run.log").open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def split_aliases(value: str | None) -> list[str]:
    if not value:
        return []
    aliases = [item.strip() for item in value.split("|") if item.strip()]
    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        key = alias.lower()
        if key not in seen:
            seen.add(key)
            result.append(alias)
    return result


def add_alias_candidate(aliases: list[str], value: str | None, prefix: str = "") -> None:
    value = clean_text(str(value or ""))
    if not value:
        return
    alias = f"{prefix}{value}" if prefix and not value.lower().startswith(prefix.strip().lower()) else value
    if alias and alias not in aliases:
        aliases.append(alias)


def load_targets() -> list[Target]:
    target_file = ROOT / CONFIG["target_file"]
    if not target_file.exists():
        target_file = ROOT / CONFIG["fallback_target_file"]
    rows = read_csv_rows(target_file)
    targets: list[Target] = []
    for row in rows:
        target_id = row.get("tic_id") or row.get("id") or row.get("target_id")
        if not target_id:
            continue
        name = row.get("literature_search_name") or row.get("simbad_main_id") or row.get("tic_name")
        aliases = split_aliases(row.get("literature_search_aliases"))
        if name and name not in aliases:
            aliases.insert(0, name)
        tic_name = row.get("tic_name") or f"TIC {target_id}"
        if tic_name not in aliases:
            aliases.append(tic_name)
        add_alias_candidate(aliases, row.get("simbad_main_id"))
        add_alias_candidate(aliases, row.get("mast_hip"), "HIP ")
        add_alias_candidate(aliases, row.get("mast_tyc"), "TYC ")
        add_alias_candidate(aliases, row.get("mast_gaia"), "Gaia DR3 ")
        add_alias_candidate(aliases, row.get("mast_twomass"), "2MASS J")
        add_alias_candidate(aliases, row.get("mast_kic"), "KIC ")
        targets.append(
            Target(
                id=str(target_id),
                name=name,
                aliases=aliases[:24],
                ra_deg=row.get("ra_deg"),
                dec_deg=row.get("dec_deg"),
                vmag=row.get("vmag"),
                teff_k=row.get("teff_k"),
                raw=row,
            )
        )
    return targets


def load_seed_hotspots() -> list[dict[str, Any]]:
    path = ROOT / CONFIG["hotspots_file"]
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def cache_path(prefix: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return ROOT / CONFIG["cache_dir"] / f"{prefix}_{digest}.json"


def read_cache(prefix: str, key: str, max_age_hours: int = 24 * 14) -> Any | None:
    path = cache_path(prefix, key)
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_cache(prefix: str, key: str, value: Any) -> None:
    ensure_dirs()
    cache_path(prefix, key).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def try_write_text(path: Path, text: str, encoding: str = "utf-8") -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return True
    except OSError as exc:
        log_event("文件写入失败", {"path": str(path), "error": str(exc)})
        return False


def build_search_terms(target: Target) -> list[str]:
    preferred = [target.name, f"TIC {target.id}"]
    aliases = target.aliases[:5]
    terms = [item for item in preferred + aliases if item]
    cleaned: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = re.sub(r"\s+", " ", term).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            cleaned.append(normalized)
    return cleaned[:5]


def parse_float(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def target_alias_terms(target: Target, limit: int | None = None) -> list[str]:
    terms = [target.name or "", f"TIC {target.id}", *target.aliases]
    cleaned: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = re.sub(r"\s+", " ", clean_text(str(term))).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            cleaned.append(normalized)
    return cleaned[:limit] if limit else cleaned


def angular_separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    ra1_rad = math.radians(ra1)
    ra2_rad = math.radians(ra2)
    dec1_rad = math.radians(dec1)
    dec2_rad = math.radians(dec2)
    cos_angle = (
        math.sin(dec1_rad) * math.sin(dec2_rad)
        + math.cos(dec1_rad) * math.cos(dec2_rad) * math.cos(ra1_rad - ra2_rad)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_angle))))


def parse_target_search_coordinates(query: str) -> dict[str, float] | None:
    text = clean_text(query)
    radius = float(CONFIG.get("target_search_neighbor_radius_deg", 2.0))
    radius_match = re.search(r"(?:radius|半径|within|附近)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:deg|degree|degrees|度)?", text, flags=re.IGNORECASE)
    if radius_match:
        radius = float(radius_match.group(1))
    labeled = re.search(
        r"\bRA\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*[,;\s]+(?:Dec|DEC|dec)\s*[:=]\s*([+-]?[0-9]+(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if labeled:
        ra = float(labeled.group(1))
        dec = float(labeled.group(2))
    else:
        pair = re.search(r"\b([0-9]{1,3}(?:\.[0-9]+)?)\s*,\s*([+-]?[0-9]{1,2}(?:\.[0-9]+)?)\b", text)
        if not pair:
            return None
        ra = float(pair.group(1))
        dec = float(pair.group(2))
    if 0 <= ra <= 360 and -90 <= dec <= 90:
        return {"ra_deg": ra, "dec_deg": dec, "radius_deg": radius}
    return None


def target_base_aliases(target: Target) -> set[str]:
    bases: set[str] = set()
    for alias in target_alias_terms(target):
        base = re.sub(r"\b(?:A|B|C|D|Aa|Ab|Ba|Bb)\b$", "", alias, flags=re.IGNORECASE).strip()
        base = re.sub(r"\s+", " ", base).lower()
        if len(base) >= 4:
            bases.add(base)
    return bases


def shared_catalog_values(target: Target) -> set[str]:
    keys = ["mast_hip", "mast_tyc", "mast_gaia", "mast_twomass", "mast_kic"]
    return {clean_text(str(target.raw.get(key) or "")).lower() for key in keys if clean_text(str(target.raw.get(key) or ""))}


def target_to_ref(target: Target, relation: str = "self", distance_deg: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": target.id,
        "name": target.name,
        "aliases": target_alias_terms(target, int(CONFIG.get("target_search_alias_limit", 10))),
        "ra_deg": parse_float(target.ra_deg),
        "dec_deg": parse_float(target.dec_deg),
        "relation": relation,
    }
    if distance_deg is not None:
        payload["distance_deg"] = round(distance_deg, 4)
    return payload


def nearby_targets(center: Target | dict[str, float], targets: list[Target], radius_deg: float, exclude_ids: set[str] | None = None) -> list[dict[str, Any]]:
    exclude_ids = exclude_ids or set()
    center_ra = parse_float(center.get("ra_deg") if isinstance(center, dict) else center.ra_deg)
    center_dec = parse_float(center.get("dec_deg") if isinstance(center, dict) else center.dec_deg)
    if center_ra is None or center_dec is None:
        return []
    nearby: list[tuple[float, Target]] = []
    for target in targets:
        if target.id in exclude_ids:
            continue
        ra = parse_float(target.ra_deg)
        dec = parse_float(target.dec_deg)
        if ra is None or dec is None:
            continue
        distance = angular_separation_deg(center_ra, center_dec, ra, dec)
        if distance <= radius_deg:
            nearby.append((distance, target))
    nearby.sort(key=lambda item: item[0])
    return [
        target_to_ref(target, "nearby_sky", distance)
        for distance, target in nearby[: int(CONFIG.get("target_search_max_related_targets", 5))]
    ]


def companion_targets(seed: Target, targets: list[Target]) -> list[dict[str, Any]]:
    seed_bases = target_base_aliases(seed)
    seed_catalog_values = shared_catalog_values(seed)
    related: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        if target.id == seed.id or target.id in seen:
            continue
        relation = ""
        if seed_bases.intersection(target_base_aliases(target)):
            relation = "host_or_companion_alias"
        elif seed_catalog_values and seed_catalog_values.intersection(shared_catalog_values(target)):
            relation = "shared_catalog_system"
        if relation:
            seen.add(target.id)
            related.append(target_to_ref(target, relation))
    return related[: int(CONFIG.get("target_search_max_related_targets", 5))]


def match_targets_for_query(query: str, targets: list[Target]) -> list[dict[str, Any]]:
    text = clean_text(query)
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        matched_aliases: list[str] = []
        if re.search(rf"(?<!\d){re.escape(target.id)}(?!\d)", text):
            matched_aliases.append(target.id)
        for alias in target_alias_terms(target):
            if len(alias) < 3:
                continue
            if entity_term_matches(text, alias):
                matched_aliases.append(alias)
        if matched_aliases and target.id not in seen:
            seen.add(target.id)
            matches.append({"target": target, "matched_aliases": matched_aliases[:5], "match_type": "alias"})
    return matches[: int(CONFIG.get("target_search_max_expanded_targets", 4))]


def build_target_search_context(query: str) -> dict[str, Any]:
    targets = load_targets()
    radius = float(CONFIG.get("target_search_neighbor_radius_deg", 2.0))
    coordinate_query = parse_target_search_coordinates(query)
    seed_matches = match_targets_for_query(query, targets)
    if coordinate_query:
        radius = float(coordinate_query.get("radius_deg", radius))
        coordinate_neighbors = nearby_targets(coordinate_query, targets, radius)
        for item in coordinate_neighbors[: int(CONFIG.get("target_search_max_expanded_targets", 4))]:
            target = next((candidate for candidate in targets if candidate.id == item["id"]), None)
            if target and not any(match["target"].id == target.id for match in seed_matches):
                seed_matches.append({"target": target, "matched_aliases": [f"坐标半径 {radius:g} deg"], "match_type": "coordinate"})
    expanded: list[dict[str, Any]] = []
    for match in seed_matches[: int(CONFIG.get("target_search_max_expanded_targets", 4))]:
        target = match["target"]
        related = companion_targets(target, targets)
        existing_ids = {target.id, *(item["id"] for item in related)}
        related.extend(nearby_targets(target, targets, radius, existing_ids))
        expanded.append(
            {
                "target": target_to_ref(target, match["match_type"]),
                "matched_aliases": match["matched_aliases"],
                "aliases": target_alias_terms(target, int(CONFIG.get("target_search_alias_limit", 10))),
                "related_targets": related[: int(CONFIG.get("target_search_max_related_targets", 5))],
            }
        )
    return {
        "enabled": bool(expanded or coordinate_query),
        "query": {
            "raw": query,
            "coordinates": coordinate_query,
            "neighbor_radius_deg": radius,
        },
        "targets": expanded,
    }


def target_from_ref(ref: dict[str, Any]) -> Target:
    return Target(
        id=str(ref.get("id") or make_target_id(str(ref.get("name") or "target"))),
        name=clean_text(str(ref.get("name") or "")) or None,
        aliases=[clean_text(str(alias)) for alias in ref.get("aliases", []) if clean_text(str(alias))],
        ra_deg=str(ref.get("ra_deg") or "") or None,
        dec_deg=str(ref.get("dec_deg") or "") or None,
    )


def cutoff_for_months(months: int | None) -> date | None:
    if not months:
        return None
    return date.today() - timedelta(days=max(1, int(months)) * 31)


def paper_date(paper: Paper) -> date | None:
    if paper.published_at:
        try:
            return datetime.fromisoformat(paper.published_at.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    if paper.year:
        return date(int(paper.year), 1, 1)
    return None


def filter_papers_by_timeframe(papers: list[Paper], timeframe_months: int | None) -> list[Paper]:
    cutoff = cutoff_for_months(timeframe_months)
    if cutoff is None:
        return papers
    return [paper for paper in papers if (paper_date(paper) or date.min) >= cutoff]


def yearly_time_buckets(timeframe_months: int | None) -> list[tuple[date, date]]:
    if not timeframe_months:
        return []
    today = date.today()
    months = int(timeframe_months)
    if months >= 12 and months % 12 == 0:
        years = max(1, months // 12)
        cutoff = date(today.year - years + 1, 1, 1)
    else:
        cutoff = cutoff_for_months(timeframe_months)
    if cutoff is None:
        return []
    buckets: list[tuple[date, date]] = []
    cursor = cutoff
    while cursor <= today:
        bucket_end = min(date(cursor.year, 12, 31), today)
        buckets.append((cursor, bucket_end))
        cursor = bucket_end + timedelta(days=1)
    return buckets


def arxiv_submitted_date_filter(start_date: date, end_date: date) -> str:
    return f"submittedDate:[{start_date:%Y%m%d}0000 TO {end_date:%Y%m%d}2359]"


def query_with_date_bucket(query: str, start_date: date, end_date: date) -> str:
    return f"({query}) AND {arxiv_submitted_date_filter(start_date, end_date)}"


def paper_in_bucket(paper: Paper, bucket: tuple[date, date]) -> bool:
    value = paper_date(paper)
    return bool(value and bucket[0] <= value <= bucket[1])


def pick_balanced_papers(papers: list[Paper], buckets: list[tuple[date, date]], limit: int | None) -> list[Paper]:
    papers = dedupe_papers(papers)
    if not limit or limit <= 0 or not buckets:
        return papers
    selected: list[Paper] = []
    selected_keys: set[str] = set()
    grouped: list[list[Paper]] = []
    for bucket in buckets:
        bucket_papers = [paper for paper in papers if paper_in_bucket(paper, bucket)]
        grouped.append(sorted(bucket_papers, key=lambda item: (item.relevance, paper_date(item) or date.min), reverse=True))
    per_bucket = max(1, math.ceil(limit / len(buckets)))
    for group in grouped:
        for paper in group[:per_bucket]:
            key = re.sub(r"\W+", "", paper.title.lower())[:80] or paper.url
            if key not in selected_keys:
                selected.append(paper)
                selected_keys.add(key)
    if len(selected) < limit:
        for paper in papers:
            key = re.sub(r"\W+", "", paper.title.lower())[:80] or paper.url
            if key not in selected_keys:
                selected.append(paper)
                selected_keys.add(key)
            if len(selected) >= limit:
                break
    selected = selected[:limit]
    ordered: list[Paper] = []
    ordered_keys: set[str] = set()
    bucket_groups = [
        sorted([paper for paper in selected if paper_in_bucket(paper, bucket)], key=lambda item: (item.relevance, paper_date(item) or date.min), reverse=True)
        for bucket in reversed(buckets)
    ]
    while len(ordered) < len(selected):
        added = False
        for group in bucket_groups:
            while group:
                paper = group.pop(0)
                key = re.sub(r"\W+", "", paper.title.lower())[:80] or paper.url
                if key not in ordered_keys:
                    ordered.append(paper)
                    ordered_keys.add(key)
                    added = True
                    break
        if not added:
            break
    for paper in selected:
        key = re.sub(r"\W+", "", paper.title.lower())[:80] or paper.url
        if key not in ordered_keys:
            ordered.append(paper)
            ordered_keys.add(key)
    return ordered[:limit]


def arxiv_search(target: Target, timeframe_months: int | None = None) -> list[Paper]:
    if not CONFIG.get("arxiv_enabled", True):
        return []
    terms = build_search_terms(target)
    query = " OR ".join(f'all:"{term}"' for term in terms[:3])
    cache_key = f"{target.id}:{query}:{CONFIG['max_papers_per_target']}:{timeframe_months or 'all'}"
    cached = read_cache("arxiv", cache_key)
    if cached is not None:
        return [Paper(**item) for item in cached]
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "start": 0,
            "max_results": int(CONFIG["max_papers_per_target"]) * 4,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    url = f"https://export.arxiv.org/api/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=6) as response:
            xml_text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
        log_event("arXiv 检索失败", {"target": target.id, "error": str(exc)})
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ElementTree.fromstring(xml_text)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ns):
        title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
        abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
        published = entry.findtext("atom:published", default="", namespaces=ns)
        authors = [clean_text(node.findtext("atom:name", default="", namespaces=ns)) for node in entry.findall("atom:author", ns)]
        link = entry.findtext("atom:id", default="", namespaces=ns)
        year = parse_year(published)
        papers.append(Paper(title, authors, abstract, published, year, link, "arXiv", target.id, relevance_for(target, title, abstract)))
    papers = filter_papers_by_timeframe(papers, timeframe_months)[: int(CONFIG["max_papers_per_target"])]
    write_cache("arxiv", cache_key, [asdict(item) for item in papers])
    return papers


def arxiv_topic_search(topic: str, timeframe_months: int | None = None, limit: int | None = None) -> list[Paper]:
    if not CONFIG.get("arxiv_enabled", True):
        return []
    topic = clean_text(topic)
    if not topic:
        return []
    max_total = None if limit is None or int(limit) <= 0 else int(limit)
    page_size = min(100, int(CONFIG.get("arxiv_page_size", 50)))
    search_terms = topic_search_terms(topic)
    query_variants = topic_query_variants(topic)
    relevance_threshold = float(CONFIG.get("topic_relevance_threshold", 0.18))
    buckets = yearly_time_buckets(timeframe_months) if max_total is not None else []
    cache_key = f"{topic}:{'|'.join(query_variants)}:{max_total or 'unlimited'}:{timeframe_months or 'all'}:balanced-v1"
    cached = read_cache("arxiv_topic", cache_key)
    if cached is not None:
        return [Paper(**item) for item in cached]
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers: list[Paper] = []
    had_request_error = False
    cutoff = cutoff_for_months(timeframe_months)
    if buckets:
        bucket_fetch_goal = max(8, math.ceil(max_total / len(buckets)) * 2)
        query_plan = [(query, bucket) for bucket in buckets for query in query_variants[:6]]
    else:
        bucket_fetch_goal = None
        query_plan = [(query, None) for query in query_variants]
    for query, bucket in query_plan:
        start = 0
        stop_query = False
        while not stop_query:
            if bucket is not None:
                bucket_count = sum(1 for paper in dedupe_papers(papers) if paper_in_bucket(paper, bucket))
                if bucket_fetch_goal is not None and bucket_count >= bucket_fetch_goal:
                    break
                search_query = query_with_date_bucket(query, bucket[0], bucket[1])
            else:
                if max_total is not None and len(dedupe_papers(papers)) >= max_total:
                    break
                search_query = query
            params = urllib.parse.urlencode(
                {
                    "search_query": search_query,
                    "start": start,
                    "max_results": page_size,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
            )
            url = f"https://export.arxiv.org/api/query?{params}"
            try:
                with urllib.request.urlopen(url, timeout=12) as response:
                    xml_text = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                had_request_error = True
                log_event("arXiv topic search failed", {"topic": topic, "query": search_query, "start": start, "error": f"HTTP {exc.code}"})
                if exc.code == 429:
                    time.sleep(8)
                break
            except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
                had_request_error = True
                log_event("arXiv topic search failed", {"topic": topic, "query": search_query, "start": start, "error": str(exc)})
                break
            root = ElementTree.fromstring(xml_text)
            entries = root.findall("atom:entry", ns)
            if not entries:
                break
            older_seen = False
            for entry in entries:
                title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
                abstract = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
                published = entry.findtext("atom:published", default="", namespaces=ns)
                authors = [clean_text(node.findtext("atom:name", default="", namespaces=ns)) for node in entry.findall("atom:author", ns)]
                link = entry.findtext("atom:id", default="", namespaces=ns)
                year = parse_year(published)
                relevance = topic_relevance(topic, title, abstract)
                if relevance < relevance_threshold:
                    continue
                paper = Paper(title, authors, abstract, published, year, link, "arXiv", "topic", relevance)
                if cutoff and paper_date(paper) and paper_date(paper) < cutoff:
                    older_seen = True
                    continue
                papers.append(paper)
            if older_seen and bucket is None:
                stop_query = True
            start += page_size
            if len(entries) < page_size:
                break
            time.sleep(1.5 if bucket is not None else 3)
    papers = filter_papers_by_timeframe(papers, timeframe_months)
    papers = pick_balanced_papers(papers, buckets, max_total) if buckets else dedupe_papers(papers)
    if max_total is not None:
        papers = papers[:max_total]
    if papers or not had_request_error:
        write_cache("arxiv_topic", cache_key, [asdict(item) for item in papers])
    return papers


def ads_search(target: Target, timeframe_months: int | None = None) -> list[Paper]:
    if not CONFIG.get("ads_enabled", True) or not os.getenv("ADS_API_KEY"):
        return []
    terms = build_search_terms(target)
    query = " OR ".join(f'"{term}"' for term in terms[:4])
    cache_key = f"{target.id}:{query}:{CONFIG['max_papers_per_target']}:{timeframe_months or 'all'}"
    cached = read_cache("ads", cache_key)
    if cached is not None:
        return [Paper(**item) for item in cached]
    params = urllib.parse.urlencode(
        {
            "q": query,
            "fl": "title,author,abstract,year,bibcode,identifier",
            "rows": int(CONFIG["max_papers_per_target"]) * 4,
            "sort": "date desc",
        }
    )
    request = urllib.request.Request(
        f"https://api.adsabs.harvard.edu/v1/search/query?{params}",
        headers={"Authorization": f"Bearer {os.getenv('ADS_API_KEY')}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log_event("ADS 检索失败", {"target": target.id, "error": str(exc)})
        return []
    papers: list[Paper] = []
    for doc in payload.get("response", {}).get("docs", []):
        title_value = doc.get("title") or [""]
        title = clean_text(title_value[0] if isinstance(title_value, list) else str(title_value))
        abstract = clean_text(doc.get("abstract") or "")
        year = int(doc["year"]) if str(doc.get("year", "")).isdigit() else None
        bibcode = doc.get("bibcode", "")
        url = f"https://ui.adsabs.harvard.edu/abs/{urllib.parse.quote(bibcode)}/abstract" if bibcode else "https://ui.adsabs.harvard.edu/"
        papers.append(
            Paper(title, doc.get("author") or [], abstract, str(year or ""), year, url, "ADS", target.id, relevance_for(target, title, abstract))
        )
    papers = filter_papers_by_timeframe(papers, timeframe_months)[: int(CONFIG["max_papers_per_target"])]
    write_cache("ads", cache_key, [asdict(item) for item in papers])
    return papers


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_year(value: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", value or "")
    return int(match.group(0)) if match else None


def relevance_for(target: Target, title: str, abstract: str) -> float:
    text = f"{title} {abstract}".lower()
    score = 0.0
    for term in build_search_terms(target):
        if term.lower() in text:
            score += 0.22
    for keyword in ["exoplanet", "habitable", "interferometry", "direct imaging", "stellar activity", "biosignature"]:
        if keyword in text:
            score += 0.08
    return min(1.0, score)


def topic_relevance(topic: str, title: str, abstract: str) -> float:
    text = f"{title} {abstract}".lower()
    tokens = set(re.findall(r"[a-z0-9+-]+", text))
    terms = topic_search_terms(topic)
    score = 0.0
    for term in terms:
        words = [word for word in re.split(r"[^a-z0-9+-]+", term.lower()) if len(word) >= 3]
        phrase_pattern = r"(?<![a-z0-9])" + r"\s+".join(re.escape(word) for word in words) + r"(?![a-z0-9])"
        if words and re.search(phrase_pattern, text):
            score += 0.45
        if words:
            hits = sum(1 for word in words if word in tokens)
            coverage = hits / len(words)
            if hits >= min(2, len(words)):
                score += 0.35 * coverage
    domain_keywords = {
        "interferometry": 0.12,
        "optical interferometry": 0.16,
        "space interferometry": 0.16,
        "formation flying": 0.14,
        "direct imaging": 0.12,
        "exoplanet": 0.12,
        "habitable": 0.08,
        "biosignature": 0.08,
        "stellar": 0.05,
        "telescope": 0.05,
    }
    for keyword, value in domain_keywords.items():
        if keyword in text:
            score += value
    if "interfer" in " ".join(terms).lower() and "interfer" not in text:
        score *= 0.35
    topic_lower = topic.lower()
    is_distributed_interferometry_topic = (
        ("\u5206\u5e03\u5f0f" in topic and "\u5e72\u6d89" in topic)
        or ("distributed" in topic_lower and "interfer" in topic_lower)
        or ("optical interfer" in topic_lower and ("space" in topic_lower or "distributed" in topic_lower))
    )
    if is_distributed_interferometry_topic:
        astronomy_context = [
            "formation",
            "baseline",
            "telescope",
            "astronom",
            "exoplanet",
            "stellar",
            " star",
            "planet",
            "habitable",
            "direct imaging",
            "nulling",
            "interferometer array",
            "space interfer",
            "space telescope",
            "spacecraft",
            "mission",
        ]
        off_topic_context = [
            "gravitational wave",
            "atom interferometry",
            "young interferometry",
            "microscope",
            "microscopy",
            "biology",
            "pathology",
            "materials science",
            "synchrotron",
            "x-ray",
            "accretion disk",
            "agn",
            "qubit",
            "quantum key",
            "quantum sensing",
            "quantum metrology",
            "wavefront sensing",
            "self-reference",
            "squeezed light",
            "tire",
        ]
        if not any(keyword in text for keyword in astronomy_context):
            score *= 0.35
        if any(keyword in text for keyword in off_topic_context):
            score *= 0.2
    if "exoplanet" in " ".join(terms).lower() and "exoplanet" not in text:
        score *= 0.5
    return round(min(1.0, score), 3)


def topic_search_terms(topic: str) -> list[str]:
    topic = clean_text(topic)
    lower = topic.lower()
    phrase_map = [
        ("\u7cfb\u5916\u5b9c\u5c45\u884c\u661f\u5927\u6c14", ["habitable exoplanet atmosphere", "habitable exoplanet atmospheric characterization", "terrestrial exoplanet atmosphere"]),
        ("\u5b9c\u5c45\u884c\u661f\u5927\u6c14", ["habitable planet atmosphere", "habitable exoplanet atmosphere"]),
        ("\u7cfb\u5916\u884c\u661f\u5927\u6c14", ["exoplanet atmosphere", "exoplanet atmospheric characterization", "exoplanet spectroscopy"]),
        ("\u5206\u5e03\u5f0f\u5149\u5e72\u6d89", ["distributed optical interferometry", "space optical interferometry", "formation flying interferometer", "space interferometer exoplanet"]),
        ("\u5149\u5e72\u6d89", ["optical interferometry", "space interferometry"]),
        ("\u76f4\u63a5\u6210\u50cf", ["direct imaging", "exoplanet direct imaging"]),
        ("\u751f\u547d\u6307\u5f81", ["biosignature", "biosignature detection", "atmospheric biosignature"]),
        ("\u5b9c\u5c45\u5e26", ["habitable zone", "habitable-zone exoplanet"]),
    ]
    terms: list[str] = []
    for chinese, english_terms in phrase_map:
        if chinese in topic:
            terms.extend(english_terms)
    word_map = {
        "\u7cfb\u5916": "exoplanet",
        "\u884c\u661f": "planet",
        "\u5b9c\u5c45": "habitable",
        "\u5927\u6c14": "atmosphere",
        "\u5149\u8c31": "spectroscopy",
        "\u5e72\u6d89": "interferometry",
        "\u6210\u50cf": "imaging",
        "\u63a2\u6d4b": "detection",
    }
    translated_words = [english for chinese, english in word_map.items() if chinese in topic]
    if translated_words and not terms:
        terms.append(" ".join(translated_words))
    if re.search(r"[a-zA-Z]", topic):
        terms.append(topic)
        words = [word for word in re.split(r"[^A-Za-z0-9+-]+", lower) if len(word) >= 3]
        if len(words) >= 2:
            terms.append(" ".join(words))
    if not terms:
        terms.append(topic)
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = re.sub(r"\s+", " ", term).strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result[:8]


def topic_query_variants(topic: str) -> list[str]:
    variants: list[str] = []
    for term in topic_search_terms(topic):
        words = [word for word in re.split(r"[^A-Za-z0-9+-]+", term) if len(word) >= 3]
        if not words:
            continue
        phrase = " ".join(words)
        variants.append(f'all:"{phrase}"')
        if len(words) == 1:
            variants.extend([f"abs:{words[0]}", f"ti:{words[0]}", f"cat:astro-ph* AND all:{words[0]}"])
        else:
            variants.append(" AND ".join(f"abs:{word}" for word in words))
            variants.append(" AND ".join(f"all:{word}" for word in words))
            variants.append(
                " OR ".join(
                    f"(abs:{words[index]} AND abs:{words[index + 1]})"
                    for index in range(min(len(words) - 1, 2))
                )
            )
    seen: set[str] = set()
    result: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            result.append(variant)
    return result[:12] or [f'all:"{clean_text(topic)}"']


def dedupe_papers(papers: list[Paper]) -> list[Paper]:
    seen: set[str] = set()
    result: list[Paper] = []
    for paper in sorted(papers, key=lambda item: (item.year or 0, item.relevance), reverse=True):
        key = re.sub(r"\W+", "", paper.title.lower())[:80] or paper.url
        if key in seen:
            continue
        seen.add(key)
        result.append(paper)
    return result


def fallback_topic_papers(topic: str, timeframe_months: int | None = None) -> list[Paper]:
    today = date.today()
    templates = [
        (
            f"Distributed optical interferometry for {topic}",
            "This paper-like placeholder summarizes formation-flying interferometers, baseline synthesis, direct imaging constraints, and science target selection.",
        ),
        (
            f"Target selection and exoplanet science enabled by {topic}",
            "This paper-like placeholder links the user topic to habitable-zone imaging, stellar sample filtering, and candidate observation prioritization.",
        ),
    ]
    return [
        Paper(
            title=title,
            authors=["Literature Assistant"],
            abstract=abstract,
            published_at=(today - timedelta(days=index * 45)).isoformat(),
            year=(today - timedelta(days=index * 45)).year,
            url="",
            source="offline",
            target_id="topic",
            relevance=0.55 - index * 0.08,
        )
        for index, (title, abstract) in enumerate(templates)
    ]


def collect_topic_papers(topic: str, timeframe_months: int | None = None, limit: int | None = None, target_context: dict[str, Any] | None = None) -> list[Paper]:
    max_topic_papers = None if limit is None or int(limit) <= 0 else int(limit)
    papers = arxiv_topic_search(topic, timeframe_months, max_topic_papers)
    context = target_context or build_target_search_context(topic)
    expanded_refs: list[dict[str, Any]] = []
    for entry in context.get("targets", []):
        expanded_refs.append(entry.get("target") or {})
        expanded_refs.extend(entry.get("related_targets") or [])
    seen_target_ids: set[str] = set()
    for ref in expanded_refs:
        target_id = str(ref.get("id") or "")
        if not target_id or target_id in seen_target_ids:
            continue
        seen_target_ids.add(target_id)
        target = target_from_ref(ref)
        target_papers = ads_search(target, timeframe_months) + arxiv_search(target, timeframe_months)
        for paper in target_papers:
            paper.relevance = max(paper.relevance, topic_relevance(topic, paper.title, paper.abstract))
            papers.append(paper)
    papers = dedupe_papers(papers)
    return papers if max_topic_papers is None else papers[:max_topic_papers]




def summarize_topic(topic: str, papers: list[Paper]) -> dict[str, Any]:
    if not papers:
        return {
            "summary": f"\u6682\u672a\u68c0\u7d22\u5230\u4e0e\u201c{topic}\u201d\u76f4\u63a5\u76f8\u5173\u7684\u6587\u732e\u3002\u8bf7\u5c1d\u8bd5\u6269\u5927\u65f6\u95f4\u8303\u56f4\u3001\u964d\u4f4e\u4e3b\u9898\u9650\u5b9a\uff0c\u6216\u8865\u5145\u82f1\u6587\u5173\u952e\u8bcd\u3002",
            "focus_points": [],
            "paper_count": 0,
        }
    combined_text = " ".join(f"{paper.title} {paper.abstract}" for paper in papers).lower()
    focus_points: list[str] = []
    keyword_map = {
        "interferometry": "\u5149\u5b66\u5e72\u6d89\u4e0e\u9635\u5217\u6210\u50cf",
        "direct imaging": "\u7cfb\u5916\u884c\u661f\u76f4\u63a5\u6210\u50cf",
        "exoplanet": "\u7cfb\u5916\u884c\u661f\u79d1\u5b66",
        "planet": "\u884c\u661f\u79d1\u5b66",
        "habitable": "\u5b9c\u5c45\u5e26\u76ee\u6807\u7b5b\u9009",
        "biosignature": "\u751f\u547d\u6307\u5f81\u63a2\u6d4b",
        "formation": "\u5206\u5e03\u5f0f\u7f16\u961f\u89c2\u6d4b",
        "atmosphere": "\u884c\u661f\u5927\u6c14\u8868\u5f81",
        "spectroscopy": "\u5149\u8c31\u63a2\u6d4b",
        "stellar": "\u6052\u661f\u6027\u8d28\u4e0e\u5bbf\u4e3b\u661f\u7ea6\u675f",
        "catalog": "\u661f\u8868\u4e0e\u6837\u672c\u6784\u5efa",
        "survey": "\u5de1\u5929\u6570\u636e\u4e0e\u7edf\u8ba1\u6837\u672c",
    }
    for keyword, label in keyword_map.items():
        if keyword in combined_text and label not in focus_points:
            focus_points.append(label)
    if not focus_points:
        focus_points = ["\u7814\u7a76\u80cc\u666f", "\u65b9\u6cd5\u8def\u7ebf", "\u5019\u9009\u76ee\u6807\u7ebf\u7d22"]
    years = [paper.year for paper in papers if paper.year]
    year_part = f"{min(years)}-{max(years)} \u5e74" if years else "\u5f53\u524d\u65f6\u95f4\u8303\u56f4\u5185"
    top_titles = "\uff1b".join(paper.title for paper in papers[:3])
    summary = (
        f"\u56f4\u7ed5\u201c{topic}\u201d\uff0c\u7cfb\u7edf\u5728{year_part}\u68c0\u7d22\u5e76\u7b5b\u9009\u51fa {len(papers)} \u7bc7\u5019\u9009\u6587\u732e\u3002"
        f"\u6587\u732e\u96c6\u4e2d\u5ea6\u6700\u9ad8\u7684\u65b9\u5411\u5305\u62ec{chr(3001).join(focus_points[:4])}\u3002"
        f"\u4ee3\u8868\u6027\u9898\u540d\u5305\u62ec\uff1a{top_titles}\u3002"
        "\u8fd9\u4e9b\u6587\u732e\u53ef\u7528\u4e8e\u5f62\u6210\u8be5\u65b9\u5411\u7684\u4e2d\u6587\u7efc\u8ff0\uff0c\u540c\u65f6\u4ece\u6458\u8981\u548c\u9898\u540d\u4e2d\u62bd\u53d6\u6052\u661f\u6216\u5bbf\u4e3b\u661f\u540d\u79f0\uff0c\u751f\u6210\u540e\u7eed\u89c2\u6d4b\u76ee\u6807\u7b5b\u9009\u5de5\u5177\u53ef\u8bfb\u53d6\u7684\u70ed\u70b9\u76ee\u6807 JSON\u3002"
    )
    return {
        "summary": summary,
        "focus_points": focus_points[:6],
        "paper_count": len(papers),
    }


def write_literature_corpus(topic: str, papers: list[Paper], timeframe_months: int | None) -> Path:
    output_path = config_path("literature_corpus_file", "outputs/literature_corpus.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Literature Corpus: {topic}",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Time range: {timeframe_months or 'all'} months",
        f"- Paper count: {len(papers)}",
        "",
    ]
    for index, paper in enumerate(papers, start=1):
        authors = ", ".join(paper.authors[:12])
        lines.extend(
            [
                f"## {index}. {paper.title}",
                "",
                f"- Year: {paper.year or 'Unknown'}",
                f"- Source: {paper.source}",
                f"- URL: {paper.url}",
                f"- Authors: {authors}",
                f"- Relevance: {paper.relevance}",
                "",
                "Abstract:",
                clean_text(paper.abstract) or "No abstract available.",
                "",
            ]
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def compact_papers_for_overview(papers: list[Paper]) -> list[dict[str, Any]]:
    abstract_chars = max(300, int(CONFIG.get("overall_analysis_abstract_chars", 900)))
    max_papers = max(1, int(CONFIG.get("overall_analysis_max_papers", 220)))
    return [
        {
            "index": index,
            "title": paper.title,
            "year": paper.year,
            "source": paper.source,
            "url": paper.url,
            "authors": paper.authors[:8],
            "relevance": paper.relevance,
            "abstract": clean_text(paper.abstract)[:abstract_chars],
        }
        for index, paper in enumerate(papers[:max_papers], start=1)
    ]


def deepseek_topic_overview(topic: str, papers: list[Paper], extracted_targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not CONFIG.get("deepseek_enabled", True) or not os.getenv("DEEPSEEK_API_KEY") or not papers:
        return None
    key_material = json.dumps(
        {
            "topic": topic,
            "papers": [asdict(paper) for paper in papers],
            "targets": [
                {
                    "name": item.get("name"),
                    "mention_count": item.get("mention_count"),
                    "related_paper_count": item.get("related_paper_count"),
                    "in_reference_catalog": bool(item.get("matched_catalog_id")),
                }
                for item in extracted_targets[:30]
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = read_cache("deepseek_topic_overview", key_material, max_age_hours=24 * 14)
    if isinstance(cached, dict):
        return cached
    prompt = {
        "topic": topic,
        "paper_count": len(papers),
        "papers": compact_papers_for_overview(papers),
        "candidate_targets": [
            {
                "name": item.get("name"),
                "mention_count": item.get("mention_count"),
                "related_paper_count": item.get("related_paper_count"),
                "in_reference_catalog": bool(item.get("matched_catalog_id")),
            }
            for item in extracted_targets[:30]
        ],
        "task": "基于这些检索文献生成中文综合分析和热点目标解释。",
        "output_schema": {
            "summary": "600-1000字中文综合分析，说明该话题近两年/当前时间段的主要研究方向、方法、趋势和限制。",
            "focus_points": ["3-8个中文研究重点"],
            "hotspot_overview": "中文说明热点目标或候选目标如何从文献中出现，若目标不足也要说明原因。",
            "target_ranking_notes": ["3-8条用于后续目标筛选/排序的建议"],
            "representative_papers": [{"index": "文献序号", "reason": "为什么代表性强"}],
        },
    }
    try:
        parsed = deepseek_chat_json(
            [
                {"role": "system", "content": "你是天文学文献综述与观测目标筛选助手。只输出可解析 JSON。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            timeout=120,
        )
        if not isinstance(parsed, dict):
            raise ValueError("DeepSeek topic overview response is not a JSON object")
        result = {
            "summary": clean_text(str(parsed.get("summary", ""))),
            "focus_points": [clean_text(str(item)) for item in parsed.get("focus_points", [])][:8],
            "hotspot_overview": clean_text(str(parsed.get("hotspot_overview", ""))),
            "target_ranking_notes": [clean_text(str(item)) for item in parsed.get("target_ranking_notes", [])][:8],
            "representative_papers": parsed.get("representative_papers", [])[:8] if isinstance(parsed.get("representative_papers", []), list) else [],
            "provider": "deepseek",
        }
        write_cache("deepseek_topic_overview", key_material, result)
        return result
    except Exception as exc:
        log_event("DeepSeek topic overview failed; falling back to local summary", {"topic": topic, "error": str(exc)})
        return None


def first_informative_sentence(abstract: str, topic_terms: list[str]) -> str:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", clean_text(abstract)) if item.strip()]
    if not sentences:
        return "\u6458\u8981\u4fe1\u606f\u8f83\u5c11\uff0c\u7cfb\u7edf\u4e3b\u8981\u4f9d\u636e\u9898\u540d\u8fdb\u884c\u5224\u65ad\u3002"
    term_words = {
        word
        for term in topic_terms
        for word in re.split(r"[^a-z0-9+-]+", term.lower())
        if len(word) >= 4
    }
    for sentence in sentences:
        lowered = sentence.lower()
        if any(word in lowered for word in term_words):
            return sentence[:320]
    return sentences[0][:320]


def paper_focus_labels(text: str) -> list[str]:
    keyword_map = [
        ("interferometry", "\u5149\u5b66\u5e72\u6d89"),
        ("formation flying", "\u7f16\u961f\u98de\u884c"),
        ("direct imaging", "\u76f4\u63a5\u6210\u50cf"),
        ("exoplanet", "\u7cfb\u5916\u884c\u661f"),
        ("planet", "\u884c\u661f\u79d1\u5b66"),
        ("habitable", "\u5b9c\u5c45\u6027"),
        ("atmosphere", "\u5927\u6c14\u8868\u5f81"),
        ("biosignature", "\u751f\u547d\u6307\u5f81"),
        ("spectroscopy", "\u5149\u8c31\u89c2\u6d4b"),
        ("stellar", "\u6052\u661f\u6027\u8d28"),
        ("catalog", "\u661f\u8868\u6784\u5efa"),
        ("survey", "\u5de1\u5929\u6837\u672c"),
        ("simulation", "\u6570\u503c\u6a21\u62df"),
        ("model", "\u6a21\u578b\u5206\u6790"),
        ("retrieval", "\u53c2\u6570\u53cd\u6f14"),
        ("telescope", "\u671b\u8fdc\u955c\u89c2\u6d4b"),
    ]
    labels: list[str] = []
    for keyword, label in keyword_map:
        if keyword in text and label not in labels:
            labels.append(label)
    return labels


def term_in_text(text: str, term: str) -> bool:
    if " " in term or "-" in term:
        pattern = r"(?<![a-z0-9])" + re.escape(term).replace(r"\ ", r"[\s-]+") + r"(?![a-z0-9])"
        return bool(re.search(pattern, text))
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))


def paper_study_object(topic: str, text: str) -> str:
    topic_lower = topic.lower()
    if ("galaxy" in topic_lower or "\u661f\u7cfb" in topic) and any(term_in_text(text, term) for term in ["galaxy", "galaxies", "reionization", "intergalactic", "star-forming galaxies"]):
        return "星系形成、星系演化或再电离相关物理"
    if ("gravitational wave" in topic_lower or "\u5f15\u529b\u6ce2" in topic) and any(term_in_text(text, term) for term in ["gravitational wave", "ringdown", "black hole", "interferometric gravitational wave"]):
        return "引力波探测、黑洞并合或干涉仪关键技术"
    if ("stellar activity" in topic_lower or "\u6052\u661f\u6d3b\u52a8" in topic) and any(term_in_text(text, term) for term in ["stellar activity", "flare", "starspot", "rotation", "x-ray", "ultraviolet"]):
        return "恒星活动、耀发或高能辐射环境"
    if ("direct imaging" in topic_lower or "\u76f4\u63a5\u6210\u50cf" in topic) and any(term_in_text(text, term) for term in ["direct imaging", "coronagraph", "high contrast", "reflected starlight"]):
        return "系外行星直接成像和高对比度观测"
    object_rules = [
        (["nulling interferometry", "kernel-nulling", "kernel nulling"], "用于系外行星探测的消光/核零干涉技术"),
        (["intensity interferometry"], "恒星目标的强度干涉成像问题"),
        (["optical interferometry", "interferometer array", "large-baseline interferometry"], "光学长基线干涉观测系统"),
        (["formation flying", "space interferometer", "distributed optical"], "空间分布式干涉阵列或编队观测方案"),
        (["lunar surface", "moon", "lunar"], "月基天文观测平台或月面设施"),
        (["host star", "stellar activity", "flare"], "\u5bbf\u4e3b\u661f\u6d3b\u52a8\u5bf9\u5927\u6c14\u89c2\u6d4b\u7684\u5f71\u54cd"),
        (["stellar activity", "flare", "starspot", "rotation"], "恒星活动、耀发或自转调制"),
        (["direct imaging", "coronagraph", "high contrast"], "系外行星直接成像和高对比度观测"),
        (["exozodiacal", "exozodi", "zodiacal dust"], "系外黄道尘结构及其对成像观测的影响"),
        (["hot jupiter", "ultra-hot jupiter"], "\u70ed\u6728\u661f\u6216\u8d85\u70ed\u6728\u661f\u5927\u6c14"),
        (["sub-neptune", "mini-neptune"], "\u4e9a\u6d77\u738b\u661f/\u5c0f\u6d77\u738b\u661f\u5927\u6c14"),
        (["super-earth", "terrestrial", "rocky"], "\u8d85\u7ea7\u5730\u7403\u6216\u5ca9\u77f3\u884c\u661f\u5927\u6c14"),
        (["brown dwarf"], "\u68d5\u77ee\u661f\u4e0e\u884c\u661f\u5927\u6c14\u7684\u5bf9\u6bd4\u6837\u672c"),
        (["m dwarf", "m-dwarf", "red dwarf"], "M \u578b\u6052\u661f\u5468\u56f4\u7684\u884c\u661f\u7cfb\u7edf"),
        (["habitable zone", "habitable-zone"], "\u5b9c\u5c45\u5e26\u884c\u661f\u6216\u5019\u9009\u5bbf\u4e3b\u661f"),
        (["biosignature", "technosignature", "seti"], "生命指征、技术指征或可居住性判据"),
        (["escape", "photoevaporation", "mass loss"], "\u884c\u661f\u5927\u6c14\u9003\u9038\u4e0e\u8d28\u91cf\u635f\u5931"),
        (["cloud", "haze", "aerosol"], "\u5927\u6c14\u4e2d\u7684\u4e91\u3001\u96fe\u973e\u6216\u6c14\u6eb6\u80f6"),
        (["water", "h2o", "methane", "ch4", "carbon dioxide", "co2"], "\u5927\u6c14\u5206\u5b50\u6210\u5206\u4e0e\u5316\u5b66\u4e30\u5ea6"),
        (["binary", "dynamical mass", "radiative transfer"], "恒星双星系统及其物理参数约束"),
        (["catalog", "sample", "target selection"], "候选目标样本、星表或筛选标准"),
        (["galaxy", "agn", "accretion disk"], "星系、活动星系核或吸积盘物理"),
        (["gravitational wave", "coating", "thin film"], "高精度干涉仪相关材料或探测器组件"),
    ]
    for keywords, label in object_rules:
        if any(term_in_text(text, keyword) for keyword in keywords):
            return label
    if "atmosphere" in text or "atmospheric" in text:
        return "\u7cfb\u5916\u884c\u661f\u5927\u6c14\u7684\u7269\u7406\u6216\u5316\u5b66\u6027\u8d28"
    if "interfer" in text:
        return "干涉测量技术及其观测应用"
    if "star" in text or "stellar" in text:
        return "恒星目标或宿主星物理性质"
    if "exoplanet" in text or "planet" in text:
        return "\u7cfb\u5916\u884c\u661f\u6837\u672c\u4e0e\u89c2\u6d4b\u7279\u5f81"
    return "\u8be5\u8bdd\u9898\u4e0b\u7684\u5019\u9009\u7814\u7a76\u95ee\u9898"


def paper_method_phrase(text: str) -> str:
    method_rules = [
        (["nulling", "kernel-nulling"], "利用消光/核零干涉抑制恒星光并提取微弱信号"),
        (["interferometry", "interferometer", "baseline"], "通过干涉测量、基线合成或相位信息分析"),
        (["image reconstruction", "generative ai", "machine learning", "neural network"], "借助机器学习或图像重建方法"),
        (["hubble space telescope", "hst", "cos"], "\u4f7f\u7528 Hubble/HST \u7d2b\u5916\u6216\u5149\u5b66\u89c2\u6d4b\u6570\u636e"),
        (["synthesize", "review", "lessons from"], "\u901a\u8fc7\u7efc\u8ff0\u3001\u7c7b\u6bd4\u6216\u8de8\u6837\u672c\u5f52\u7eb3"),
        (["jwst", "nircam", "nirspec", "miri"], "\u4f7f\u7528 JWST \u6216\u5176\u7ea2\u5916\u4eea\u5668\u6570\u636e"),
        (["transmission spectrum", "transmission spectroscopy", "transit spectroscopy"], "\u901a\u8fc7\u51cc\u661f/\u900f\u5c04\u5149\u8c31\u5206\u6790"),
        (["emission spectrum", "secondary eclipse", "phase curve"], "\u5229\u7528\u53d1\u5c04\u5149\u8c31\u3001\u6b21\u98df\u6216\u76f8\u4f4d\u66f2\u7ebf"),
        (["high-resolution spectroscopy", "cross-correlation"], "\u91c7\u7528\u9ad8\u5206\u8fa8\u7387\u5149\u8c31\u548c\u4e92\u76f8\u5173\u65b9\u6cd5"),
        (["retrieval", "bayesian", "nested sampling", "mcmc"], "\u57fa\u4e8e\u53c2\u6570\u53cd\u6f14\u6216\u8d1d\u53f6\u65af\u5efa\u6a21"),
        (["simulation", "synthetic", "mock"], "\u501f\u52a9\u6a21\u62df\u6216\u5408\u6210\u89c2\u6d4b\u8bc4\u4f30"),
        (["model", "grid", "chemistry", "radiative transfer"], "\u901a\u8fc7\u5927\u6c14\u6a21\u578b\u3001\u5316\u5b66\u6216\u8f90\u5c04\u8f6c\u79fb\u8ba1\u7b97"),
        (["catalog", "survey", "population"], "\u57fa\u4e8e\u661f\u8868\u3001\u5de1\u5929\u6216\u7edf\u8ba1\u6837\u672c"),
        (["direct imaging", "coronagraph"], "\u9762\u5411\u76f4\u63a5\u6210\u50cf\u6216\u9ad8\u5bf9\u6bd4\u5ea6\u89c2\u6d4b"),
        (["radial velocity", "transit", "light curve"], "利用径向速度、凌星或光变曲线数据"),
        (["reverberation mapping", "cross correlation"], "采用时延、互相关或时域响应分析"),
        (["laboratory", "experiment", "measurement"], "通过实验测量或仪器性能评估"),
    ]
    for keywords, label in method_rules:
        if any(term_in_text(text, keyword) for keyword in keywords):
            return label
    if "spect" in text:
        return "\u4f9d\u636e\u5149\u8c31\u89c2\u6d4b\u6216\u5149\u8c31\u5efa\u6a21"
    if "observ" in text:
        return "\u57fa\u4e8e\u89c2\u6d4b\u6570\u636e\u8fdb\u884c\u5206\u6790"
    return "\u7ed3\u5408\u9898\u540d\u4e0e\u6458\u8981\u4e2d\u7684\u7814\u7a76\u7ebf\u7d22"


def paper_contribution_phrase(topic: str, text: str) -> str:
    contribution_rules = [
        (["nulling interferometry", "kernel-nulling", "direct exoplanet detection"], "它直接关联高对比度探测能力，可用于判断哪些宿主星系统更适合后续直接成像或干涉观测。"),
        (["intensity interferometry", "image reconstruction"], "它为恒星尺度结构重建和高角分辨成像提供方法线索，可帮助评估目标是否具备可观测结构。"),
        (["lunar surface", "moon", "space interferometer", "formation flying"], "它提供空间/平台设计和观测条件方面的信息，可用于理解任务构型对目标选择的限制。"),
        (["exozodiacal", "zodiacal dust"], "它说明尘埃背景如何影响宜居带行星成像，是筛选宿主星时需要考虑的噪声来源。"),
        (["stellar activity", "flare", "starspot"], "它提醒目标排序时需要考虑宿主星活动性，否则行星信号和大气信号可能被恒星噪声污染。"),
        (["target selection", "catalog", "sample"], "它更适合作为目标样本构建依据，可直接服务于后续热点目标列表和排序规则。"),
        (["escape", "photoevaporation", "mass loss"], "\u5b83\u91cd\u70b9\u8bf4\u660e\u8f90\u7167\u3001\u6052\u661f\u6d3b\u52a8\u6216\u91cd\u529b\u6761\u4ef6\u5982\u4f55\u6539\u53d8\u5927\u6c14\u4fdd\u7559\u80fd\u529b\u3002"),
        (["cloud", "haze", "aerosol"], "\u5b83\u5bf9\u89e3\u91ca\u5149\u8c31\u4e2d\u7684\u9000\u5316\u3001\u4e91\u96fe\u906e\u853d\u548c\u5927\u6c14\u7ed3\u6784\u6709\u76f4\u63a5\u53c2\u8003\u4ef7\u503c\u3002"),
        (["water", "h2o", "methane", "ch4", "co2", "carbon dioxide"], "\u5b83\u63d0\u4f9b\u4e86\u5206\u5b50\u5438\u6536\u7279\u5f81\u548c\u5316\u5b66\u7ec4\u6210\u5224\u522b\u7684\u7ebf\u7d22\u3002"),
        (["jwst", "nirspec", "nircam", "miri"], "\u5b83\u53cd\u6620\u4e86\u5f53\u524d JWST \u65f6\u4ee3\u5927\u6c14\u8868\u5f81\u7684\u6570\u636e\u8d28\u91cf\u3001\u6837\u672c\u548c\u7cfb\u7edf\u8bef\u5dee\u95ee\u9898\u3002"),
        (["retrieval", "bayesian", "mcmc"], "\u5b83\u6709\u52a9\u4e8e\u7406\u89e3\u5927\u6c14\u53c2\u6570\u53cd\u6f14\u7684\u5148\u9a8c\u3001\u9000\u5316\u548c\u53ef\u4fe1\u5ea6\u8bc4\u4f30\u3002"),
        (["host star", "stellar activity", "flare"], "\u5b83\u63d0\u9192\u5728\u7b5b\u9009\u70ed\u70b9\u76ee\u6807\u65f6\u8981\u540c\u65f6\u8003\u8651\u5bbf\u4e3b\u661f\u6d3b\u52a8\u6027\u548c\u89c2\u6d4b\u7a33\u5b9a\u6027\u3002"),
        (["catalog", "survey", "population"], "\u5b83\u66f4\u9002\u5408\u7528\u6765\u6784\u5efa\u5019\u9009\u6837\u672c\u548c\u5224\u65ad\u54ea\u4e9b\u5bbf\u4e3b\u661f\u503c\u5f97\u540e\u7eed\u8ddf\u8e2a\u3002"),
        (["direct imaging", "coronagraph"], "\u5b83\u628a\u5927\u6c14\u7814\u7a76\u4e0e\u76f4\u63a5\u6210\u50cf\u53ef\u89c2\u6d4b\u6027\u8054\u7cfb\u8d77\u6765\u3002"),
        (["biosignature", "technosignature", "seti"], "它扩展了科学解释目标，可帮助区分大气表征、生命指征和技术指征等不同观测动机。"),
        (["binary", "dynamical mass"], "它提供恒星基本参数约束，对判断目标物理性质和观测优先级有辅助意义。"),
    ]
    for keywords, sentence in contribution_rules:
        if any(term_in_text(text, keyword) for keyword in keywords):
            return sentence
    topic_terms = topic_search_terms(topic)
    topic_hint = topic_terms[0] if topic_terms else topic
    return f"它与“{topic_hint}”的联系主要来自题名和摘要中的问题设定，可作为综合分析中的一条候选证据。"


def paper_value_for_topic(topic: str, text: str) -> str:
    lower_topic = topic.lower()
    if "\u5e72\u6d89" in topic or "interfer" in lower_topic:
        if "exoplanet" in text or "planet" in text:
            return "\u5b83\u628a\u5e72\u6d89\u89c2\u6d4b\u80fd\u529b\u4e0e\u884c\u661f/\u5bbf\u4e3b\u661f\u79d1\u5b66\u95ee\u9898\u8fde\u63a5\u8d77\u6765\uff0c\u53ef\u5e2e\u52a9\u5224\u65ad\u54ea\u4e9b\u6052\u661f\u7cfb\u7edf\u503c\u5f97\u4f5c\u4e3a\u540e\u7eed\u89c2\u6d4b\u76ee\u6807\u3002"
        return "\u5b83\u63d0\u4f9b\u4e86\u5e72\u6d89\u6d4b\u91cf\u3001\u9635\u5217\u6784\u578b\u6216\u9ad8\u89d2\u5206\u8fa8\u89c2\u6d4b\u65b9\u9762\u7684\u6280\u672f\u7ebf\u7d22\uff0c\u53ef\u4f5c\u4e3a\u7406\u89e3\u5206\u5e03\u5f0f\u5149\u5e72\u6d89\u65b9\u6848\u7684\u80cc\u666f\u6750\u6599\u3002"
    if "\u5927\u6c14" in topic or "atmosphere" in lower_topic:
        return "\u5b83\u6709\u52a9\u4e8e\u7406\u89e3\u884c\u661f\u5927\u6c14\u89c2\u6d4b\u3001\u5149\u8c31\u8868\u5f81\u6216\u53c2\u6570\u53cd\u6f14\u4e2d\u7684\u5173\u952e\u95ee\u9898\uff0c\u5e76\u4e3a\u5bbf\u4e3b\u661f\u7b5b\u9009\u63d0\u4f9b\u4e0a\u4e0b\u6587\u3002"
    if "\u7cfb\u5916" in topic or "exoplanet" in lower_topic:
        return "\u5b83\u53ef\u5e2e\u52a9\u8bc6\u522b\u8fd1\u671f\u7cfb\u5916\u884c\u661f\u7814\u7a76\u4e2d\u7684\u6d3b\u8dc3\u5bbf\u4e3b\u661f\u3001\u6837\u672c\u9009\u62e9\u903b\u8f91\u548c\u89c2\u6d4b\u4f18\u5148\u7ea7\u3002"
    return "\u5b83\u4e3a\u8be5\u8bdd\u9898\u63d0\u4f9b\u4e86\u7814\u7a76\u95ee\u9898\u3001\u65b9\u6cd5\u6216\u6837\u672c\u7ebf\u7d22\uff0c\u53ef\u7eb3\u5165\u7efc\u5408\u5206\u6790\u4e0e\u70ed\u70b9\u76ee\u6807\u62bd\u53d6\u3002"


def fallback_paper_chinese_summary(topic: str, paper: Paper) -> str:
    text = f"{paper.title} {paper.abstract}".lower()
    labels = paper_focus_labels(text)
    focus = "\u3001".join(labels[:3]) if labels else "\u7814\u7a76\u95ee\u9898\u3001\u65b9\u6cd5\u8def\u7ebf\u548c\u79d1\u5b66\u610f\u4e49"
    evidence = first_informative_sentence(paper.abstract, topic_search_terms(topic))
    year = str(paper.year) if paper.year else "\u672a\u77e5\u5e74\u4efd"
    study_object = paper_study_object(topic, text)
    method = paper_method_phrase(text)
    contribution = paper_contribution_phrase(topic, text)
    return (
        f"\u8fd9\u7bc7 {year} \u5e74\u6587\u732e\u9898\u4e3a\u300a{paper.title}\u300b\u3002"
        f"\u6458\u8981\u4e2d\u7684\u5173\u952e\u4fe1\u606f\u662f\uff1a{evidence} "
        f"\u8fd9\u7bc7\u6587\u732e\u7684\u6838\u5fc3\u5bf9\u8c61\u662f{study_object}\uff0c\u4e3b\u8981\u65b9\u6cd5\u662f{method}\u3002"
        f"\u5173\u952e\u6807\u7b7e\u53ef\u5f52\u4e3a{focus}\u3002{contribution}"
    )


def paper_chinese_summary(topic: str, paper: Paper) -> str:
    return fallback_paper_chinese_summary(topic, paper)


def paper_summary_cache_key(topic: str, paper: Paper) -> str:
    material = json.dumps(
        {
            "topic": topic,
            "title": paper.title,
            "abstract": paper.abstract,
            "year": paper.year,
            "source": paper.source,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def parse_json_content(content: str) -> Any:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return json.loads(value.strip())


def deepseek_chat_json(messages: list[dict[str, str]], timeout: int = 80) -> Any:
    request_body = {
        "model": os.getenv("DEEPSEEK_MODEL", CONFIG.get("deepseek_model", "deepseek-chat")),
        "temperature": 0.2,
        "messages": messages,
    }
    request = urllib.request.Request(
        os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions"),
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_json_content(payload["choices"][0]["message"]["content"])


def deepseek_paper_summaries(topic: str, papers: list[Paper]) -> dict[str, str]:
    summaries: dict[str, str] = {}
    if not CONFIG.get("deepseek_enabled", True) or not CONFIG.get("deepseek_paper_summary_enabled", True) or not os.getenv("DEEPSEEK_API_KEY"):
        return summaries
    pending: list[tuple[str, Paper]] = []
    for paper in papers:
        key = paper_summary_cache_key(topic, paper)
        cached = read_cache("deepseek_paper_summary", key, max_age_hours=24 * 60)
        if isinstance(cached, dict) and cached.get("summary"):
            summaries[key] = str(cached["summary"])
        else:
            pending.append((key, paper))
    if not pending:
        return summaries
    batch_size = max(1, int(CONFIG.get("paper_summary_batch_size", 8)))
    abstract_chars = max(300, int(CONFIG.get("paper_summary_abstract_chars", 1800)))
    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        paper_payload = [
            {
                "index": index,
                "title": paper.title,
                "year": paper.year,
                "source": paper.source,
                "authors": paper.authors[:8],
                "abstract": clean_text(paper.abstract)[:abstract_chars],
                "url": paper.url,
            }
            for index, (_, paper) in enumerate(batch)
        ]
        prompt = {
            "topic": topic,
            "papers": paper_payload,
            "requirements": [
                "请为每篇论文生成独立中文摘要，不要套用统一模板。",
                "摘要必须基于该论文题名和英文摘要本身，说明研究问题、方法或数据、主要结论/贡献，以及它与检索话题的关系。",
                "每篇 120 到 220 个中文字；如果摘要信息不足，请明确说明依据有限。",
                "只输出 JSON 数组，数组元素格式为 {\"index\": 数字, \"summary\": \"中文摘要\"}。",
            ],
        }
        try:
            parsed = deepseek_chat_json(
                [
                    {"role": "system", "content": "你是严谨的天文学文献综述助手。你只输出可解析 JSON，不输出 Markdown。"},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                timeout=90,
            )
            if isinstance(parsed, dict):
                parsed = parsed.get("summaries") or parsed.get("items") or []
            if not isinstance(parsed, list):
                raise ValueError("DeepSeek paper summary response is not a JSON array")
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                index = int(item.get("index", -1))
                summary = clean_text(str(item.get("summary", "")))
                if 0 <= index < len(batch) and summary:
                    key, _paper = batch[index]
                    summaries[key] = summary
                    write_cache("deepseek_paper_summary", key, {"summary": summary, "provider": "deepseek"})
        except Exception as exc:
            log_event("DeepSeek paper summaries failed; falling back to local summaries", {"topic": topic, "error": str(exc), "batch_start": batch_start})
            continue
    return summaries


def build_paper_summary_map(topic: str, papers: list[Paper]) -> dict[str, str]:
    summary_map = deepseek_paper_summaries(topic, papers)
    for paper in papers:
        key = paper_summary_cache_key(topic, paper)
        if key not in summary_map:
            summary_map[key] = fallback_paper_chinese_summary(topic, paper)
    return summary_map


def paper_from_payload(payload: dict[str, Any]) -> Paper:
    return Paper(
        title=clean_text(str(payload.get("title") or "")),
        authors=[clean_text(str(item)) for item in payload.get("authors", []) if str(item).strip()][:20],
        abstract=clean_text(str(payload.get("abstract") or "")),
        published_at=clean_text(str(payload.get("published_at") or "")),
        year=int(payload["year"]) if str(payload.get("year", "")).isdigit() else None,
        url=clean_text(str(payload.get("url") or "")),
        source=clean_text(str(payload.get("source") or "文献源")),
        target_id=clean_text(str(payload.get("target_id") or payload.get("targetId") or "topic")),
        relevance=float(payload.get("relevance") or 0),
    )


def paper_records_path() -> Path:
    return config_path("paper_records_file", "outputs/paper_records.json")


def load_paper_records() -> dict[str, PaperRecord]:
    path = paper_records_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = list(data.values())
    else:
        items = []
    records: dict[str, PaperRecord] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("paper_id"):
            continue
        try:
            records[str(item["paper_id"])] = PaperRecord(**item)
        except TypeError:
            allowed = {field.name for field in PaperRecord.__dataclass_fields__.values()}
            records[str(item["paper_id"])] = PaperRecord(**{key: value for key, value in item.items() if key in allowed})
    return records


def save_paper_records(records: dict[str, PaperRecord]) -> None:
    ensure_dirs()
    payload = [asdict(record) for record in sorted(records.values(), key=lambda item: item.paper_id)]
    paper_records_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def science_records_path() -> Path:
    return config_path("science_records_file", "outputs/science_records.json")


def load_science_records() -> dict[str, ScienceRecord]:
    path = science_records_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    items = data if isinstance(data, list) else list(data.values()) if isinstance(data, dict) else []
    records: dict[str, ScienceRecord] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("paper_id"):
            continue
        try:
            records[str(item["paper_id"])] = ScienceRecord(**item)
        except TypeError:
            allowed = {field.name for field in ScienceRecord.__dataclass_fields__.values()}
            records[str(item["paper_id"])] = ScienceRecord(**{key: value for key, value in item.items() if key in allowed})
    return records


def save_science_records(records: dict[str, ScienceRecord]) -> None:
    ensure_dirs()
    payload = [asdict(record) for record in sorted(records.values(), key=lambda item: item.paper_id)]
    science_records_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_science_record(record: ScienceRecord) -> ScienceRecord:
    records = load_science_records()
    record.updated_at = datetime.now().isoformat(timespec="seconds")
    records[record.paper_id] = record
    save_science_records(records)
    return record


def extract_arxiv_id(value: str) -> tuple[str | None, str | None]:
    value = clean_text(value)
    if not value:
        return None, None
    patterns = [
        r"arxiv\.org/(?:abs|pdf)/([^?#\s]+)",
        r"\barXiv:([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b",
        r"\b([A-Za-z.-]+/\d{7}|\d{4}\.\d{4,5})(v\d+)?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).removesuffix(".pdf")
        version_match = re.search(r"(v\d+)$", raw, flags=re.IGNORECASE)
        version = match.group(2) if len(match.groups()) >= 2 and match.group(2) else None
        if version_match:
            version = version_match.group(1)
            raw = raw[: -len(version)]
        return raw, version
    return None, None


def extract_doi(value: str) -> str | None:
    match = re.search(r"\b10\.\d{4,9}/[^\s\"<>]+", value or "", flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def paper_identifier_payload(paper: Paper, payload: dict[str, Any] | None = None) -> dict[str, str | None]:
    payload = payload or {}
    source_text = " ".join(
        clean_text(str(value))
        for value in [
            payload.get("arxiv_id"),
            payload.get("doi"),
            payload.get("url"),
            payload.get("pdf_url"),
            payload.get("source_url"),
            paper.url,
            paper.title,
        ]
        if value
    )
    arxiv_id, version = extract_arxiv_id(source_text)
    doi = clean_text(str(payload.get("doi") or "")) or extract_doi(source_text)
    return {"arxiv_id": arxiv_id, "version": version, "doi": doi}


def paper_id_for(paper: Paper, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    if payload.get("paper_id"):
        return clean_text(str(payload["paper_id"]))
    identifiers = paper_identifier_payload(paper, payload)
    if identifiers["arxiv_id"]:
        return "arxiv_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", identifiers["arxiv_id"])
    if identifiers["doi"]:
        return "doi_" + hashlib.sha256(str(identifiers["doi"]).lower().encode("utf-8")).hexdigest()[:18]
    key = json.dumps({"title": paper.title, "url": paper.url}, ensure_ascii=False, sort_keys=True)
    return "paper_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:18]


def safe_filename(value: str, limit: int = 96) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return (cleaned or "paper")[:limit]


def relative_path(path: Path) -> str:
    return config_path_display(path)


def arxiv_pdf_url(arxiv_id: str, version: str | None = None) -> str:
    suffix = version or ""
    return f"https://arxiv.org/pdf/{arxiv_id}{suffix}.pdf"


def arxiv_candidates_from_title(title: str) -> list[str]:
    title = clean_text(title)
    if not title:
        return []
    query_title = re.sub(r"[^\w\s:+-]", " ", title, flags=re.UNICODE)
    query_title = re.sub(r"\s+", " ", query_title).strip()
    if not query_title:
        return []
    params = urllib.parse.urlencode(
        {
            "search_query": f'ti:"{query_title[:180]}"',
            "start": 0,
            "max_results": 3,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
    )
    try:
        with urllib.request.urlopen(f"https://export.arxiv.org/api/query?{params}", timeout=10) as response:
            xml_text = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
        log_event("arXiv title PDF lookup failed", {"title": title, "error": str(exc)})
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ElementTree.fromstring(xml_text)
    candidates: list[str] = []
    for entry in root.findall("atom:entry", ns):
        link = entry.findtext("atom:id", default="", namespaces=ns)
        arxiv_id, version = extract_arxiv_id(link)
        if arxiv_id:
            candidates.append(arxiv_pdf_url(arxiv_id, version))
    return candidates


def pdf_candidate_links(paper: Paper, payload: dict[str, Any] | None = None) -> list[str]:
    payload = payload or {}
    candidates: list[str] = []
    for key in ["pdf_url", "open_access_url", "source_url", "url"]:
        value = clean_text(str(payload.get(key) or ""))
        if value and (value.endswith(".pdf") or "/pdf/" in value or "arxiv.org/pdf" in value):
            candidates.append(value)
    if paper.url and (paper.url.endswith(".pdf") or "/pdf/" in paper.url or "arxiv.org/pdf" in paper.url):
        candidates.append(paper.url)
    identifiers = paper_identifier_payload(paper, payload)
    if identifiers["arxiv_id"]:
        candidates.append(arxiv_pdf_url(str(identifiers["arxiv_id"]), identifiers["version"]))
    if identifiers["doi"]:
        candidates.append(f"https://doi.org/{identifiers['doi']}")
    has_direct_pdf = any(url.endswith(".pdf") or "/pdf/" in url or "arxiv.org/pdf" in url for url in candidates)
    if not identifiers["arxiv_id"] and not has_direct_pdf:
        candidates.extend(arxiv_candidates_from_title(paper.title))
    result: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        key = url.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


def download_pdf_bytes(url: str) -> tuple[bytes | None, str | None, str | None]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "LiteratureAssistant/1.0 (+https://local)",
            "Accept": "application/pdf,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            final_url = response.geturl()
            max_bytes = int(CONFIG.get("max_pdf_bytes", 80 * 1024 * 1024))
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                return None, "PDF 文件超过大小上限", final_url
            if body[:5] == b"%PDF-" or "application/pdf" in content_type:
                return body, None, final_url
            return None, f"链接未返回 PDF（Content-Type: {content_type or 'unknown'}）", final_url
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}", url
    except (urllib.error.URLError, TimeoutError, http.client.IncompleteRead) as exc:
        return None, str(exc), url


def parse_multipart_form(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if "multipart/form-data" not in (content_type or "").lower():
        raise ValueError("请求必须使用 multipart/form-data")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise ValueError("未识别到上传表单内容")
    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_param("filename", header="content-disposition")
        content = part.get_payload(decode=True) or b""
        if filename is not None:
            files[str(name)] = {
                "filename": str(filename),
                "content_type": part.get_content_type(),
                "content": content,
            }
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[str(name)] = content.decode(charset, errors="replace")
    return fields, files


def validate_pdf_upload(body: bytes) -> str | None:
    if not body:
        return "上传文件为空"
    max_bytes = int(CONFIG.get("max_pdf_bytes", 80 * 1024 * 1024))
    if len(body) > max_bytes:
        return "PDF 文件超过大小上限"
    if not body[:1024].lstrip().startswith(b"%PDF-"):
        return "上传文件不是有效 PDF（缺少 %PDF- 文件头）"
    return None


def save_uploaded_paper_pdf(paper: Paper, payload: dict[str, Any], pdf_body: bytes, original_filename: str = "") -> PaperRecord:
    ensure_dirs()
    validation_error = validate_pdf_upload(pdf_body)
    if validation_error:
        raise ValueError(validation_error)
    records = load_paper_records()
    paper_id = paper_id_for(paper, payload)
    existing = records.get(paper_id)
    identifiers = paper_identifier_payload(paper, payload)
    uploaded_name = Path(original_filename or "uploaded.pdf").name
    source_label = f"local-upload:{uploaded_name}"
    title = paper.title or Path(uploaded_name).stem
    filename_stem = safe_filename(Path(uploaded_name or title).stem, 80)
    filename = f"{safe_filename(paper_id)}_{filename_stem}.pdf"
    pdf_path = config_path("pdf_dir", "outputs/pdfs") / filename
    pdf_path.write_bytes(pdf_body)
    candidate_links = list(dict.fromkeys((existing.candidate_links if existing else []) + [source_label]))
    record = PaperRecord(
        paper_id=paper_id,
        title=title,
        arxiv_id=identifiers["arxiv_id"],
        doi=identifiers["doi"],
        version=identifiers["version"],
        source_url=source_label,
        download_time=datetime.now().isoformat(timespec="seconds"),
        fetch_status="success",
        failure_reason=None,
        candidate_links=candidate_links,
        pdf_path=relative_path(pdf_path),
        markdown_path=None,
        parse_status="not_started",
        parse_error=None,
        parse_time=None,
        parser=None,
    )
    records[paper_id] = record
    save_paper_records(records)
    log_event("paper PDF uploaded", {"paper_id": paper_id, "filename": original_filename, "pdf_path": record.pdf_path})
    return record


def fetch_and_save_paper_pdf(paper: Paper, payload: dict[str, Any] | None = None) -> PaperRecord:
    payload = payload or {}
    ensure_dirs()
    records = load_paper_records()
    paper_id = paper_id_for(paper, payload)
    existing = records.get(paper_id)
    if existing and existing.fetch_status == "success" and existing.pdf_path and stored_file_path(existing.pdf_path).exists():
        return existing
    identifiers = paper_identifier_payload(paper, payload)
    candidates = pdf_candidate_links(paper, payload)
    record = PaperRecord(
        paper_id=paper_id,
        title=paper.title,
        arxiv_id=identifiers["arxiv_id"],
        doi=identifiers["doi"],
        version=identifiers["version"],
        fetch_status="fetching",
        candidate_links=candidates,
        parse_status=existing.parse_status if existing else "not_started",
        markdown_path=existing.markdown_path if existing else None,
    )
    if not candidates:
        record.fetch_status = "no_open_fulltext"
        record.failure_reason = "未发现 arXiv、DOI 或开放 PDF 候选链接"
        records[paper_id] = record
        save_paper_records(records)
        log_event("paper PDF fetch failed", {"paper_id": paper_id, "title": paper.title, "reason": record.failure_reason})
        return record
    errors: list[str] = []
    for candidate in candidates:
        body, error, final_url = download_pdf_bytes(candidate)
        if body:
            filename = f"{safe_filename(paper_id)}_{safe_filename(paper.title, 80)}.pdf"
            pdf_path = config_path("pdf_dir", "outputs/pdfs") / filename
            pdf_path.write_bytes(body)
            record.source_url = final_url or candidate
            record.download_time = datetime.now().isoformat(timespec="seconds")
            record.fetch_status = "success"
            record.failure_reason = None
            record.pdf_path = relative_path(pdf_path)
            records[paper_id] = record
            save_paper_records(records)
            log_event("paper PDF fetched", {"paper_id": paper_id, "source_url": record.source_url, "pdf_path": record.pdf_path})
            return record
        errors.append(f"{candidate}: {error or '未知错误'}")
    record.fetch_status = "download_failed"
    record.failure_reason = "；".join(errors[:3]) or "下载失败"
    records[paper_id] = record
    save_paper_records(records)
    log_event("paper PDF fetch failed", {"paper_id": paper_id, "title": paper.title, "reason": record.failure_reason})
    return record


def paper_record_payload(paper: Paper, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    paper_id = paper_id_for(paper, payload)
    record = load_paper_records().get(paper_id)
    return asdict(record) if record else None


def parse_tasks_dir() -> Path:
    return config_path("parse_tasks_dir", "outputs/parse_tasks")


def parse_task_path(task_id: str) -> Path:
    return parse_tasks_dir() / f"{safe_filename(task_id, 80)}.json"


def load_parse_task(task_id: str) -> ParseTask | None:
    path = parse_task_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    try:
        return ParseTask(**data)
    except TypeError:
        allowed = {field.name for field in ParseTask.__dataclass_fields__.values()}
        return ParseTask(**{key: value for key, value in data.items() if key in allowed})


def save_parse_task(task: ParseTask) -> None:
    ensure_dirs()
    task.updated_at = datetime.now().isoformat(timespec="seconds")
    parse_task_path(task.task_id).write_text(json.dumps(asdict(task), ensure_ascii=False, indent=2), encoding="utf-8")


def latest_parse_task_for_paper(paper_id: str, statuses: set[str] | None = None) -> ParseTask | None:
    ensure_dirs()
    latest: ParseTask | None = None
    latest_time = ""
    for task_file in parse_tasks_dir().glob("*.json"):
        try:
            data = json.loads(task_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if data.get("paper_id") != paper_id:
            continue
        if statuses and data.get("status") not in statuses:
            continue
        task = load_parse_task(str(data.get("task_id") or ""))
        if not task:
            continue
        task_time = task.updated_at or task.created_at or ""
        if not latest or task_time > latest_time:
            latest = task
            latest_time = task_time
    return refresh_parse_task(latest) if latest else None


def parse_lock_path() -> Path:
    return parse_tasks_dir() / "mineru_parse.lock"


def acquire_parse_lock(task: ParseTask, max_wait_seconds: int = 60 * 60 * 3) -> bool:
    start = time.time()
    lock_path = parse_lock_path()
    while True:
        try:
            with lock_path.open("x", encoding="utf-8") as file:
                file.write(json.dumps({"task_id": task.task_id, "paper_id": task.paper_id, "time": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False))
            return True
        except FileExistsError:
            if time.time() - start > max_wait_seconds:
                task.status = "failed"
                task.parse_status = "failed"
                task.message = "等待 MinerU 解析锁超时"
                task.completed_at = datetime.now().isoformat(timespec="seconds")
                save_parse_task(task)
                return False
            task.status = "queued"
            task.parse_status = "queued"
            task.progress_percent = 0
            task.progress_stage = "等待其他 MinerU 任务完成"
            task.message = "已有 MinerU 任务运行中，当前任务排队等待"
            save_parse_task(task)
            time.sleep(3)


def release_parse_lock(task_id: str) -> None:
    lock_path = parse_lock_path()
    if not lock_path.exists():
        return
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    if data.get("task_id") in {None, task_id}:
        try:
            lock_path.unlink()
        except OSError:
            pass


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def resolve_record_pdf_path(record: PaperRecord) -> Path | None:
    if not record.pdf_path:
        return None
    pdf_path = stored_file_path(record.pdf_path).resolve()
    allowed_roots = [
        config_path("pdf_dir", "outputs/pdfs").resolve(),
        (ROOT / clean_text(str(CONFIG.get("pdf_dir") or "outputs/pdfs"))).resolve(),
    ]
    if not any(is_path_relative_to(pdf_path, root) for root in allowed_roots):
        return None
    return pdf_path if pdf_path.exists() else None


def resolve_record_markdown_path(record: PaperRecord) -> Path | None:
    if not record.markdown_path:
        return None
    md_path = stored_file_path(record.markdown_path).resolve()
    allowed_roots = [
        config_path("markdown_dir", "outputs/markdown").resolve(),
        (ROOT / clean_text(str(CONFIG.get("markdown_dir") or "outputs/markdown"))).resolve(),
    ]
    if not any(is_path_relative_to(md_path, root) for root in allowed_roots):
        return None
    return md_path if md_path.exists() else None


def paper_record_for_paper(paper: Paper) -> PaperRecord | None:
    return load_paper_records().get(paper_id_for(paper))


def paper_text_source(paper: Paper, max_chars: int | None = None) -> tuple[str, str, str | None]:
    record = paper_record_for_paper(paper)
    max_chars = max_chars or int(CONFIG.get("science_markdown_max_chars", 120000))
    if record and record.parse_status in {"success", "need_review"}:
        md_path = resolve_record_markdown_path(record)
        if md_path:
            text = md_path.read_text(encoding="utf-8", errors="replace")
            return text[:max_chars], "markdown", record.paper_id
    fallback = "\n\n".join([paper.title, paper.abstract])
    return fallback[:max_chars], "abstract", paper_id_for(paper)


def markdown_sections(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_title = "全文"
    current_lines: list[str] = []
    for line in (text or "").splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if heading:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append({"section": current_title, "text": content})
            current_title = clean_text(re.sub(r"#+$", "", heading.group(1)))
            current_lines = []
        else:
            current_lines.append(line)
    content = "\n".join(current_lines).strip()
    if content:
        sections.append({"section": current_title, "text": content})
    if not sections and text.strip():
        sections.append({"section": "全文", "text": text.strip()})
    return sections


def split_evidence_sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or " ").strip()
    if not cleaned:
        return []
    pieces = re.split(r"(?<=[.!?。！？])\s+", cleaned)
    return [piece.strip() for piece in pieces if len(piece.strip()) >= 20]


def evidence_ref(paper_id: str, section: str, quote: str) -> dict[str, Any]:
    return asdict(EvidenceRef(paper_id=paper_id, section=section or "全文", quote=quote[:600], page=None))


def add_science_evidence(record: ScienceRecord, kind: str, label: str, section: str, quote: str) -> int:
    ref = evidence_ref(record.paper_id, section, quote)
    ref["kind"] = kind
    ref["label"] = label
    key = (
        str(ref.get("kind") or "").lower(),
        str(ref.get("label") or "").lower(),
        str(ref.get("section") or "").lower(),
        re.sub(r"\s+", " ", str(ref.get("quote") or "")).strip().lower(),
    )
    for index, existing in enumerate(record.evidence):
        existing_key = (
            str(existing.get("kind") or "").lower(),
            str(existing.get("label") or "").lower(),
            str(existing.get("section") or "").lower(),
            re.sub(r"\s+", " ", str(existing.get("quote") or "")).strip().lower(),
        )
        if existing_key == key:
            return index
    record.evidence.append(ref)
    return len(record.evidence) - 1


def entity_term_pattern(value: str) -> str | None:
    value = re.sub(r"\s+", " ", value or "").strip()
    if not value:
        return None
    parts = [part for part in re.split(r"\s+", value) if part]
    escaped = r"[\s-]+".join(re.escape(part) for part in parts)
    return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"


def entity_term_matches(text: str, value: str) -> list[re.Match[str]]:
    pattern = entity_term_pattern(value)
    if not pattern:
        return []
    return list(re.finditer(pattern, text or "", flags=re.IGNORECASE))


def append_unique_ref(item: dict[str, Any], ref_index: int) -> None:
    refs = item.setdefault("evidence_refs", [])
    if ref_index not in refs and len(refs) < 3:
        refs.append(ref_index)


def extract_target_mentions_from_text(paper_id: str, text: str, sections: list[dict[str, str]], record: ScienceRecord) -> list[dict[str, Any]]:
    patterns = [
        r"\bTIC\s*\d+\b",
        r"\bTOI[-\s]?\d+(?:\.\d+)?[a-z]?\b",
        r"\bHD\s*\d+[A-Z]?\b",
        r"\bHIP\s*\d+\b",
        r"\bGJ\s*\d+[A-Za-z]?\b",
        r"\bGliese\s*\d+[A-Za-z]?\b",
        r"\bK2[-\s]?\d+[A-Za-z]?\b",
        r"\bKepler[-\s]?\d+[a-z]?\b",
        r"\bTRAPPIST[-\s]?1[a-z]?\b",
        r"\bProxima\s+Centauri\b",
        r"\bAlpha\s+Centauri\b",
        r"\bTau\s+Ceti\b",
        r"\bProcyon\b",
        r"\bLHS\s*\d+\b",
        r"\bWolf\s*\d+\b",
    ]
    alias_lookup: list[tuple[str, Target]] = []
    seen_aliases: set[tuple[str, str]] = set()
    for target in load_targets():
        for alias in [target.name or "", f"TIC {target.id}", *target.aliases]:
            alias = alias.strip()
            alias_key = (target.id, alias.lower())
            if len(alias) >= 4 and alias_key not in seen_aliases:
                seen_aliases.add(alias_key)
                alias_lookup.append((alias, target))
    found: dict[str, dict[str, Any]] = {}
    for section in sections:
        section_text = section["text"]
        sentences = split_evidence_sentences(section_text)
        for pattern in patterns:
            for match in re.finditer(pattern, section_text, flags=re.IGNORECASE):
                raw = re.sub(r"\s+", " ", match.group(0)).strip()
                name = normalize_stellar_target_name(raw)
                key = name.lower()
                quote = next((sentence for sentence in sentences if raw.lower() in sentence.lower()), clean_text(section_text)[:300])
                item = found.setdefault(key, {"name": name, "aliases": [], "mention_count": 0, "evidence_refs": []})
                if raw not in item["aliases"]:
                    item["aliases"].append(raw)
                item["mention_count"] += 1
                append_unique_ref(item, add_science_evidence(record, "target", name, section["section"], quote))
        for alias, target in alias_lookup:
            matches = entity_term_matches(section_text, alias)
            if not matches:
                continue
            key = (target.name or alias).lower()
            quote = next((sentence for sentence in sentences if entity_term_matches(sentence, alias)), clean_text(section_text)[:300])
            item = found.setdefault(
                key,
                {
                    "name": target.name or alias,
                    "aliases": [],
                    "catalog_id": target.id,
                    "mention_count": 0,
                    "evidence_refs": [],
                },
            )
            item["catalog_id"] = target.id
            if alias not in item["aliases"]:
                item["aliases"].append(alias)
            item["mention_count"] += len(matches)
            append_unique_ref(item, add_science_evidence(record, "target", target.name or alias, section["section"], quote))
    return sorted(found.values(), key=lambda item: item.get("mention_count", 0), reverse=True)[:12]


def extract_methods_from_sections(sections: list[dict[str, str]], record: ScienceRecord) -> list[dict[str, Any]]:
    method_rules = [
        ("transit spectroscopy", ["transit spectroscopy", "transmission spectrum", "transmission spectroscopy"]),
        ("radial velocity", ["radial velocity", "RV measurements", "HARPS", "ESPRESSO"]),
        ("direct imaging", ["direct imaging", "coronagraph", "high contrast imaging"]),
        ("optical interferometry", ["optical interferometry", "interferometer", "nulling interferometry", "VLTI", "CHARA"]),
        ("photometry", ["photometry", "light curve", "TESS", "Kepler", "K2"]),
        ("spectroscopy", ["spectroscopy", "spectrograph", "NIRSpec", "MIRI", "JWST", "HST"]),
        ("astrometry", ["astrometry", "Gaia"]),
        ("simulation/modeling", ["simulation", "modeling", "retrieval", "radiative transfer", "MCMC", "Bayesian"]),
    ]
    found: dict[str, dict[str, Any]] = {}
    for section in sections:
        sentences = split_evidence_sentences(section["text"])
        lower = section["text"].lower()
        for label, keywords in method_rules:
            matched = [keyword for keyword in keywords if keyword.lower() in lower]
            if not matched:
                continue
            quote = next((sentence for sentence in sentences if any(keyword.lower() in sentence.lower() for keyword in matched)), clean_text(section["text"])[:300])
            item = found.setdefault(label, {"name": label, "keywords": [], "evidence_refs": []})
            for keyword in matched:
                if keyword not in item["keywords"]:
                    item["keywords"].append(keyword)
            if len(item["evidence_refs"]) < 3:
                item["evidence_refs"].append(add_science_evidence(record, "method", label, section["section"], quote))
    return list(found.values())[:10]


def extract_parameters_from_sections(sections: list[dict[str, str]], record: ScienceRecord) -> list[dict[str, Any]]:
    parameter_specs = [
        ("distance", r"\b(?:distance|d)\s*(?:=|is|of|:)?\s*(?:about|approximately|~)?\s*([0-9]+(?:\.[0-9]+)?)\s*(pc|parsec|parsecs|ly|light[- ]years)\b"),
        ("radius", r"\b(?:radius|R[_\s]?[A-Za-z0-9*⊙☉]+?)\s*(?:=|is|of|:)?\s*(?:about|approximately|~)?\s*([0-9]+(?:\.[0-9]+)?)\s*(R[_\s]?(?:sun|Sun|⊙|☉|Jup|Jupiter|Earth|⊕)|solar radii|Earth radii|Jupiter radii)\b"),
        ("mass", r"\b(?:mass|M[_\s]?[A-Za-z0-9*⊙☉]+?)\s*(?:=|is|of|:)?\s*(?:about|approximately|~)?\s*([0-9]+(?:\.[0-9]+)?)\s*(M[_\s]?(?:sun|Sun|⊙|☉|Jup|Jupiter|Earth|⊕)|solar masses|Earth masses|Jupiter masses)\b"),
        ("temperature", r"\b(?:temperature|T[_\s]?eff|effective temperature|T_eff)\s*(?:=|is|of|:)?\s*(?:about|approximately|~)?\s*([0-9]{3,6}(?:\.[0-9]+)?)\s*(K|kelvin)\b"),
        ("orbital_period", r"\b(?:orbital period|period|P)\s*(?:=|is|of|:)?\s*(?:about|approximately|~)?\s*([0-9]+(?:\.[0-9]+)?)\s*(days|day|d|hours|hr|yrs|yr|years)\b"),
    ]
    found: dict[str, dict[str, Any]] = {
        name: {"name": name, "value": "", "unit": "", "target": "", "evidence_refs": []}
        for name, _pattern in parameter_specs
    }
    for section in sections:
        sentences = split_evidence_sentences(section["text"])
        for name, pattern in parameter_specs:
            if found[name]["value"]:
                continue
            match = re.search(pattern, section["text"], flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group(1)
            unit = match.group(2)
            quote = next((sentence for sentence in sentences if match.group(0).lower() in sentence.lower()), clean_text(section["text"])[:300])
            found[name]["value"] = value
            found[name]["unit"] = unit
            found[name]["evidence_refs"].append(add_science_evidence(record, "parameter", name, section["section"], quote))
    return list(found.values())


def extract_conclusions_from_sections(sections: list[dict[str, str]], record: ScienceRecord) -> list[dict[str, Any]]:
    preferred_sections = [section for section in sections if re.search(r"conclusion|summary|discussion|result", section["section"], flags=re.IGNORECASE)]
    candidate_sections = preferred_sections or sections[-3:]
    conclusion_keywords = ["we find", "we found", "we conclude", "we show", "our results", "suggest", "indicate", "demonstrate", "conclude"]
    conclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in candidate_sections:
        for sentence in split_evidence_sentences(section["text"]):
            lower = sentence.lower()
            if not any(keyword in lower for keyword in conclusion_keywords):
                continue
            key = sentence[:120].lower()
            if key in seen:
                continue
            seen.add(key)
            evidence_index = add_science_evidence(record, "conclusion", "conclusion", section["section"], sentence)
            conclusions.append({"text": sentence[:500], "evidence_refs": [evidence_index]})
            if len(conclusions) >= 5:
                return conclusions
    return conclusions


METHOD_CN_LABELS = {
    "transit spectroscopy": "凌星/透射光谱",
    "radial velocity": "径向速度",
    "direct imaging": "直接成像",
    "optical interferometry": "光学干涉",
    "photometry": "光度测量",
    "spectroscopy": "光谱观测",
    "astrometry": "天体测量",
    "simulation/modeling": "模拟与模型反演",
}


PARAMETER_CN_LABELS = {
    "distance": "距离",
    "radius": "半径",
    "mass": "质量",
    "temperature": "有效温度",
    "orbital_period": "轨道周期",
}


def science_item_label(item: dict[str, Any], labels: dict[str, str]) -> str:
    name = clean_text(str(item.get("name") or ""))
    return labels.get(name, name or "-")


def local_science_analysis(record: ScienceRecord) -> dict[str, Any]:
    target_names = [clean_text(str(item.get("name") or "")) for item in record.targets if item.get("name")]
    method_names = [science_item_label(item, METHOD_CN_LABELS) for item in record.methods]
    observed_params = [
        science_item_label(item, PARAMETER_CN_LABELS)
        for item in record.parameters
        if clean_text(str(item.get("value") or ""))
    ]
    source_label = "MinerU Markdown 全文" if record.source == "markdown" else "题名与摘要"
    summary_parts = [f"本次科学信息提取基于{source_label}，已保留原文证据片段用于回溯。"]
    if target_names:
        summary_parts.append(f"文中明确出现的研究对象包括：{'、'.join(target_names[:6])}。")
    if method_names:
        summary_parts.append(f"方法层面主要涉及：{'、'.join(method_names[:6])}。")
    if observed_params:
        summary_parts.append(f"可直接读出的物理参数包括：{'、'.join(observed_params[:6])}。")
    if not target_names and not method_names and not observed_params:
        summary_parts.append("当前规则抽取未找到高置信度对象、方法或白名单参数，建议结合原文证据复核。")
    return {
        "provider": "local_rules",
        "summary_cn": "".join(summary_parts),
        "research_object_cn": "、".join(target_names[:8]) if target_names else "未识别到明确研究对象。",
        "methods_cn": "、".join(method_names[:8]) if method_names else "未识别到明确观测方法或仪器。",
        "parameters_cn": "、".join(observed_params[:8]) if observed_params else "未识别到白名单物理参数。",
        "conclusions_cn": "已抽取结论原文片段，请结合下方证据阅读。" if record.conclusions else "未识别到明确结论句。",
    }


def deepseek_science_analysis(record: ScienceRecord, text: str) -> dict[str, Any] | None:
    if not CONFIG.get("deepseek_enabled", True) or not os.getenv("DEEPSEEK_API_KEY"):
        return None
    excerpt = text[:50000]
    key_material = json.dumps(
        {
            "mode": "science_cn_analysis_v1",
            "paper_id": record.paper_id,
            "source": record.source,
            "extractor_version": record.extractor_version,
            "record": {
                "targets": record.targets,
                "methods": record.methods,
                "parameters": record.parameters,
                "conclusions": record.conclusions,
                "evidence": record.evidence[:20],
            },
            "text_hash": hashlib.sha1(excerpt.encode("utf-8", errors="ignore")).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = read_cache("deepseek_science_analysis", key_material, max_age_hours=24 * 30)
    if isinstance(cached, dict):
        return cached
    prompt = {
        "text_source": record.source,
        "full_text_or_abstract": excerpt,
        "rule_extracted_science_record": asdict(record),
        "requirements": [
            "请用中文给出跨领域读者容易理解的科学信息分析。",
            "分析结论必须来自 full_text_or_abstract 或 rule_extracted_science_record，不要编造。",
            "原文证据片段不要翻译，保留其原始语言；你只输出中文分析字段。",
            "公式、物理量和单位请保留 Markdown 公式写法，例如 $R_p/R_*$ 或 $$F_\\nu$$，不要把公式解释改写成无法渲染的纯文本。",
            "输出 JSON 对象。",
        ],
        "output_schema": {
            "summary_cn": "150-300字中文总览",
            "research_object_cn": "中文说明研究对象及其语义",
            "methods_cn": "中文说明观测/数据/模型方法",
            "parameters_cn": "中文说明关键物理参数及意义",
            "conclusions_cn": "中文说明主要结论与限制",
        },
    }
    try:
        parsed = deepseek_chat_json(
            [
                {"role": "system", "content": "你是严谨的天文学文献科学信息抽取助手。只输出可解析 JSON。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            timeout=100,
        )
        if not isinstance(parsed, dict):
            raise ValueError("science analysis response is not a JSON object")
        result = {
            "provider": "deepseek",
            "summary_cn": clean_text(str(parsed.get("summary_cn") or "")),
            "research_object_cn": clean_text(str(parsed.get("research_object_cn") or "")),
            "methods_cn": clean_text(str(parsed.get("methods_cn") or "")),
            "parameters_cn": clean_text(str(parsed.get("parameters_cn") or "")),
            "conclusions_cn": clean_text(str(parsed.get("conclusions_cn") or "")),
        }
        if result["summary_cn"]:
            write_cache("deepseek_science_analysis", key_material, result)
            return result
    except Exception as exc:
        log_event("DeepSeek science analysis failed; using local science summary", {"paper_id": record.paper_id, "error": str(exc)})
    return None


def extract_science_from_text(paper_id: str, text: str, source: str = "abstract") -> ScienceRecord:
    sections = markdown_sections(text)
    record = ScienceRecord(paper_id=paper_id, source=source)
    record.targets = extract_target_mentions_from_text(paper_id, text, sections, record)
    record.methods = extract_methods_from_sections(sections, record)
    record.parameters = extract_parameters_from_sections(sections, record)
    record.conclusions = extract_conclusions_from_sections(sections, record)
    record.analysis = deepseek_science_analysis(record, text) or local_science_analysis(record)
    return record


def science_record_for_paper(paper: Paper, force: bool = False) -> ScienceRecord:
    paper_id = paper_id_for(paper)
    text, source, resolved_paper_id = paper_text_source(paper)
    existing = load_science_records().get(paper_id)
    if existing and not force and existing.extractor_version == SCIENCE_EXTRACTOR_VERSION and not (existing.source != "markdown" and source == "markdown"):
        return existing
    record = extract_science_from_text(resolved_paper_id or paper_id, text, source)
    return save_science_record(record)


def science_record_for_record(record: PaperRecord, force: bool = False) -> ScienceRecord:
    existing = load_science_records().get(record.paper_id)
    md_path = resolve_record_markdown_path(record)
    source = "markdown" if md_path else "abstract"
    if existing and not force and existing.extractor_version == SCIENCE_EXTRACTOR_VERSION and not (existing.source != "markdown" and source == "markdown"):
        return existing
    if md_path:
        text = md_path.read_text(encoding="utf-8", errors="replace")[: int(CONFIG.get("science_markdown_max_chars", 120000))]
    else:
        text = record.title or record.paper_id
    return save_science_record(extract_science_from_text(record.paper_id, text, source))


def mineru_runtime_dir(record: PaperRecord) -> Path:
    path = config_path("parse_tasks_dir", "outputs/parse_tasks").parent / "mineru_runtime" / safe_filename(record.paper_id, 80)
    path.mkdir(parents=True, exist_ok=True)
    return path


def mineru_safe_pdf_path(record: PaperRecord, source_pdf_path: Path) -> Path:
    runtime_dir = mineru_runtime_dir(record)
    target = runtime_dir / "input.pdf"
    try:
        if not target.exists() or target.stat().st_size != source_pdf_path.stat().st_size:
            shutil.copy2(source_pdf_path, target)
    except OSError:
        shutil.copy2(source_pdf_path, target)
    return target


def find_markdown_output(output_dir: Path, preferred_path: Path) -> Path | None:
    if preferred_path.exists() and preferred_path.stat().st_size > 0:
        return preferred_path
    candidates = [path for path in output_dir.rglob("*.md") if path.is_file() and path.stat().st_size > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.stat().st_size, path.stat().st_mtime), reverse=True)
    return candidates[0]


def markdown_needs_review(markdown_path: Path) -> str | None:
    try:
        text = markdown_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Markdown 读取失败：{exc}"
    stripped = text.strip()
    if len(stripped) < 500:
        return "Markdown 文本过短，需人工复核"
    headings = len(re.findall(r"^#{1,6}\s+", stripped, flags=re.MULTILINE))
    if headings < 2:
        return "Markdown 章节结构较弱，需人工复核"
    return None


def read_log_tail(path: Path, max_chars: int = 1200) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:].strip()


def config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def mineru_models_config(adapter_config: dict[str, Any]) -> dict[str, Any]:
    config = dict(CONFIG.get("mineru_models") or {})
    nested = adapter_config.get("models") if isinstance(adapter_config, dict) else None
    if isinstance(nested, dict):
        config.update(nested)
    return config


def resolve_mineru_model_downloader(adapter_config: dict[str, Any]) -> str:
    model_config = mineru_models_config(adapter_config)
    candidates: list[str] = []
    configured = clean_text(str(model_config.get("command") or ""))
    if configured:
        candidates.append(configured)
    adapter_command = clean_text(str(adapter_config.get("command") or "mineru"))
    if adapter_command:
        command_path = Path(adapter_command)
        if command_path.exists():
            suffix = ".exe" if command_path.suffix.lower() == ".exe" else ""
            candidates.append(str(command_path.with_name(f"mineru-models-download{suffix}")))
    if os.name == "nt":
        candidates.append(str(ROOT / ".mineru-venv" / "Scripts" / "mineru-models-download.exe"))
    candidates.append("mineru-models-download")
    for candidate in candidates:
        executable = shutil.which(candidate) or (candidate if Path(candidate).exists() else "")
        if executable:
            return executable
    return ""


def prepare_mineru_models(adapter_config: dict[str, Any], output_dir: Path) -> tuple[bool, str | None]:
    model_config = mineru_models_config(adapter_config)
    if not config_bool(model_config.get("auto_prepare"), True):
        return True, None
    downloader = resolve_mineru_model_downloader(adapter_config)
    if not downloader:
        return False, "未找到 MinerU 模型下载器 mineru-models-download；请先安装 MinerU 或运行 prepare_mineru_models.ps1"
    source = clean_text(str(model_config.get("source") or "modelscope"))
    model_type = clean_text(str(model_config.get("model_type") or "pipeline"))
    timeout = int(model_config.get("timeout_seconds") or 7200)
    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "mineru-models-download.stdout.log"
    stderr_path = output_dir / "mineru-models-download.stderr.log"
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    command = [downloader, "-s", source, "-m", model_type]
    started = datetime.now().isoformat(timespec="seconds")
    try:
        with stdout_path.open("a", encoding="utf-8") as stdout_file, stderr_path.open("a", encoding="utf-8") as stderr_file:
            stdout_file.write(f"\n[{started}] Preparing MinerU models: source={source}, model_type={model_type}\n")
            stdout_file.flush()
            result = subprocess.run(
                command,
                cwd=str(ROOT),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                timeout=timeout,
                env=env,
                shell=False,
            )
    except subprocess.TimeoutExpired:
        return False, f"MinerU 模型准备超时（>{timeout}s）"
    except OSError as exc:
        return False, f"MinerU 模型下载器启动失败：{exc}"
    if result.returncode != 0:
        message = read_log_tail(stderr_path, max_chars=2400) or read_log_tail(stdout_path, max_chars=2400)
        return False, f"MinerU 模型准备失败（退出码 {result.returncode}）：{message or '未返回错误信息'}"
    return True, None


def parse_size_to_mb(value: str, unit: str) -> float:
    number = float(value)
    unit = (unit or "").upper()
    if unit == "K":
        return number / 1024
    if unit == "G":
        return number * 1024
    if unit == "T":
        return number * 1024 * 1024
    return number


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", value or "")


def mineru_api_token() -> str:
    return clean_text(os.getenv("MINERU_API_TOKEN", ""))


def mineru_api_base(config: dict[str, Any]) -> str:
    return clean_text(str(config.get("api_base") or "https://mineru.net")).rstrip("/")


def mineru_config_payload() -> dict[str, Any]:
    adapter = dict(CONFIG.get("mineru_adapter") or {})
    storage_base = clean_text(str(CONFIG.get("storage_base_dir") or ""))
    return {
        "enabled": bool(mineru_api_token()),
        "token_preview": "已配置" if mineru_api_token() else "未配置",
        "mode": clean_text(str(adapter.get("mode") or "command")),
        "command": clean_text(str(adapter.get("command") or "mineru")),
        "service_url": clean_text(str(adapter.get("service_url") or "")),
        "api_base": mineru_api_base(adapter),
        "api_model_version": clean_text(str(adapter.get("api_model_version") or "vlm")),
        "api_language": clean_text(str(adapter.get("api_language") or "en")),
        "api_enable_formula": config_bool(adapter.get("api_enable_formula"), True),
        "api_enable_table": config_bool(adapter.get("api_enable_table"), True),
        "api_poll_interval_seconds": int(adapter.get("api_poll_interval_seconds") or 5),
        "storage_base_dir": storage_base,
        "resolved_storage_base_dir": str(configured_storage_base() or (ROOT / "outputs")),
        "paths": {
            "pdf_dir": str(config_path("pdf_dir", "outputs/pdfs")),
            "markdown_dir": str(config_path("markdown_dir", "outputs/markdown")),
            "parse_tasks_dir": str(config_path("parse_tasks_dir", "outputs/parse_tasks")),
            "paper_records_file": str(config_path("paper_records_file", "outputs/paper_records.json")),
            "science_records_file": str(config_path("science_records_file", "outputs/science_records.json")),
            "output_file": str(config_path("output_file", "outputs/hotspots.json")),
        },
    }


def mineru_api_json_request(config: dict[str, Any], method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    token = mineru_api_token()
    if not token:
        raise RuntimeError("未配置 MINERU_API_TOKEN，请先在 .env 中填写 MinerU API Token")
    url = mineru_api_base(config) + path
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    attempts = max(1, int(config.get("api_request_retries") or 3))
    last_error: BaseException | None = None
    body = ""
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Connection": "close",
                "User-Agent": "LiteratureAssistant/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            raise RuntimeError(f"MinerU API 返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
            last_error = exc
            log_event("MinerU API request failed; retrying", {"method": method, "path": path, "attempt": attempt, "error": str(exc)})
            if attempt < attempts:
                time.sleep(min(2 * attempt, 6))
                continue
    else:
        raise RuntimeError(f"MinerU API 请求失败：{last_error}") from last_error
    try:
        result = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MinerU API 返回非 JSON：{body[:400]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("MinerU API 返回格式异常")
    code = result.get("code")
    if code not in {None, 0, "0", 200, "200"} and result.get("ok") is not True:
        message = result.get("msg") or result.get("message") or result.get("error") or result
        raise RuntimeError(f"MinerU API 调用失败：{message}")
    return result


def mineru_api_payload_data(result: dict[str, Any]) -> Any:
    data = result.get("data")
    if data is None:
        data = result.get("result")
    return data if data is not None else result


def mineru_download_bytes(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "LiteratureAssistant/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"下载 MinerU 结果失败 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"下载 MinerU 结果失败：{exc}") from exc


def mineru_oss_put_bytes(upload_url: str, body: bytes, timeout: int = 300) -> None:
    parsed = urllib.parse.urlsplit(upload_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("MinerU 上传地址无效")
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.netloc, timeout=timeout)
    try:
        connection.putrequest("PUT", path, skip_accept_encoding=True)
        connection.putheader("Host", parsed.netloc)
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader("Connection", "close")
        connection.endheaders()
        for start in range(0, len(body), 1024 * 1024):
            connection.send(body[start : start + 1024 * 1024])
        response = connection.getresponse()
        response_body = response.read().decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(f"上传 PDF 到 MinerU 失败 HTTP {response.status}：{response_body[:1200]}")
    finally:
        connection.close()


def extract_markdown_from_zip_bytes(archive_bytes: bytes) -> str:
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".md")]
        if not names:
            raise RuntimeError("MinerU 结果压缩包中未找到 Markdown 文件")
        names.sort(key=lambda name: (Path(name).name.lower() != "full.md", len(name)))
        with archive.open(names[0]) as file:
            return file.read().decode("utf-8", errors="replace")


def first_present(value: Any, keys: list[str]) -> Any:
    if not isinstance(value, dict):
        return None
    for key in keys:
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, str) and item == "":
            continue
        return item
    return None


def is_ssl_eof_error(exc: BaseException) -> bool:
    text = str(exc)
    return "EOF occurred in violation of protocol" in text or "UNEXPECTED_EOF_WHILE_READING" in text


def infer_parse_progress(task: ParseTask) -> ParseTask:
    if task.status not in {"queued", "running"}:
        return task
    output_dir = config_path("markdown_dir", "outputs/markdown") / safe_filename(task.paper_id, 120)
    stderr_text = strip_ansi(read_log_tail(output_dir / "mineru.stderr.log", max_chars=24000))
    stdout_text = strip_ansi(read_log_tail(output_dir / "mineru.stdout.log", max_chars=4000))
    model_stdout_text = strip_ansi(read_log_tail(output_dir / "mineru-models-download.stdout.log", max_chars=24000))
    model_stderr_text = strip_ansi(read_log_tail(output_dir / "mineru-models-download.stderr.log", max_chars=12000))
    text = "\n".join([stderr_text, stdout_text, model_stdout_text, model_stderr_text])
    if task.status == "queued":
        task.progress_percent = task.progress_percent if task.progress_percent is not None else 0
        task.progress_stage = task.progress_stage or "排队等待"
        return task
    task.progress_percent = task.progress_percent if task.progress_percent is not None else 2
    task.progress_stage = task.progress_stage or "启动 MinerU"
    download_matches = re.findall(r"([\w.-]+):[^\n\r]*?(\d+(?:\.\d+)?)([KMGT]?)/(\d+(?:\.\d+)?)([KMGT]?)", text, flags=re.IGNORECASE)
    if download_matches:
        name, current, current_unit, total, total_unit = download_matches[-1]
        current_mb = parse_size_to_mb(current, current_unit)
        total_mb = max(0.001, parse_size_to_mb(total, total_unit))
        percent = max(0.0, min(100.0, current_mb / total_mb * 100))
        task.progress_percent = round(percent, 1)
        task.progress_stage = f"下载模型 {name} {current}{current_unit}/{total}{total_unit} ({percent:.1f}%)"
        return task
    if "Preparing MinerU models" in text or "Downloading MinerU models" in text:
        task.progress_percent = max(float(task.progress_percent or 0), 2)
        task.progress_stage = "准备 MinerU 模型"
        return task
    if "Still waiting to acquire lock" in text:
        task.progress_percent = max(float(task.progress_percent or 0), 1)
        task.progress_stage = "等待模型下载锁，已有任务正在下载模型"
        return task
    page_matches = re.findall(r"(\d+)\s*/\s*(\d+)\s+pages", text, flags=re.IGNORECASE)
    if page_matches:
        done, total = page_matches[-1]
        total_int = max(1, int(total))
        percent = int(done) / total_int * 100
        task.progress_percent = round(percent, 1)
        task.progress_stage = f"处理页面 {done}/{total}"
        return task
    if "DocAnalysis init" in text:
        task.progress_percent = max(float(task.progress_percent or 0), 8)
        task.progress_stage = "初始化解析模型"
    elif "Submitting batch" in text:
        task.progress_percent = max(float(task.progress_percent or 0), 5)
        task.progress_stage = "提交 PDF 批次"
    elif "Started local mineru-api" in text or "Start MinerU FastAPI" in text:
        task.progress_percent = max(float(task.progress_percent or 0), 3)
        task.progress_stage = "启动本地 MinerU 服务"
    return task


def refresh_parse_task(task: ParseTask) -> ParseTask:
    if task.status == "running" and task.worker_pid and not process_is_alive(task.worker_pid):
        task.status = "failed"
        task.parse_status = "failed"
        task.message = "MinerU 解析进程已中断，请重新点击解析PDF"
        task.progress_percent = None
        task.progress_stage = None
        task.completed_at = datetime.now().isoformat(timespec="seconds")
        records = load_paper_records()
        record = records.get(task.paper_id)
        if record:
            updated = update_record_after_parse(record, "failed", None, task.message)
            task.record = asdict(updated)
        save_parse_task(task)
        release_parse_lock(task.task_id)
        return task
    return infer_parse_progress(task)


class MinerUAdapter:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def _update_task(self, task: ParseTask | None, percent: float, stage: str, message: str | None = None) -> None:
        if not task:
            return
        task.progress_percent = max(0, min(100, percent))
        task.progress_stage = stage
        if message:
            task.message = message
        save_parse_task(task)

    def parse(self, record: PaperRecord, task: ParseTask | None = None) -> tuple[str, Path | None, str | None]:
        pdf_path = resolve_record_pdf_path(record)
        if not pdf_path:
            return "failed", None, "PDF 文件不存在或路径无效，请先重新获取 PDF"
        output_dir = config_path("markdown_dir", "outputs/markdown") / safe_filename(record.paper_id, 120)
        output_dir.mkdir(parents=True, exist_ok=True)
        preferred_path = output_dir / f"{safe_filename(record.paper_id, 120)}.md"
        mode = clean_text(str(self.config.get("mode") or "command")).lower()
        if mode in {"official_api", "api", "mineru_api"}:
            return self._parse_with_official_api(record, pdf_path, output_dir, preferred_path, task)
        if mode == "service":
            return self._parse_with_service(record, pdf_path, output_dir, preferred_path)
        models_ready, models_message = prepare_mineru_models(self.config, output_dir)
        if not models_ready:
            return "failed", None, models_message
        safe_pdf_path = mineru_safe_pdf_path(record, pdf_path)
        return self._parse_with_command(record, safe_pdf_path, output_dir, preferred_path)

    def _template_context(self, record: PaperRecord, pdf_path: Path, output_dir: Path, markdown_path: Path) -> dict[str, str]:
        return {
            "paper_id": record.paper_id,
            "pdf_path": str(pdf_path),
            "output_dir": str(output_dir),
            "markdown_path": str(markdown_path),
        }

    def _parse_with_command(self, record: PaperRecord, pdf_path: Path, output_dir: Path, markdown_path: Path) -> tuple[str, Path | None, str | None]:
        command = clean_text(str(self.config.get("command") or "mineru"))
        executable = shutil.which(command) or (command if Path(command).exists() else "")
        if not executable:
            return "need_review", None, f"未找到 MinerU 命令：{command}。请在 config.json 配置 mineru_adapter.command"
        raw_args = self.config.get("args") or ["-p", "{pdf_path}", "-o", "{output_dir}"]
        context = self._template_context(record, pdf_path, output_dir, markdown_path)
        args = [str(arg).format(**context) for arg in raw_args]
        timeout = int(self.config.get("timeout_seconds") or 600)
        stdout_path = output_dir / "mineru.stdout.log"
        stderr_path = output_dir / "mineru.stderr.log"
        env = os.environ.copy()
        runtime_tmp = mineru_runtime_dir(record) / "tmp"
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        env["TMP"] = str(runtime_tmp)
        env["TEMP"] = str(runtime_tmp)
        env["TMPDIR"] = str(runtime_tmp)
        env.setdefault("PYTHONUTF8", "1")
        compat_path = str(ROOT / "mineru_compat")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = compat_path if not existing_pythonpath else compat_path + os.pathsep + existing_pythonpath
        fast_lang_model = clean_text(str(self.config.get("fast_langdetect_model_path") or CONFIG.get("fast_langdetect_model_path") or ""))
        if not fast_lang_model:
            fast_lang_model = str(Path.home() / ".cache" / "litassis" / "fast_langdetect" / "lid.176.ftz")
        if Path(fast_lang_model).exists():
            env["FTLANG_SMALL_MODEL"] = fast_lang_model
        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
                result = subprocess.run(
                    [executable, *args],
                    cwd=str(ROOT),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    timeout=timeout,
                    env=env,
                    shell=False,
                )
        except subprocess.TimeoutExpired:
            return "failed", None, f"MinerU 解析超时（>{timeout}s）"
        except OSError as exc:
            return "failed", None, f"MinerU 命令启动失败：{exc}"
        if result.returncode != 0:
            message = read_log_tail(stderr_path) or read_log_tail(stdout_path)
            return "failed", None, f"MinerU 命令退出码 {result.returncode}：{message or '未返回错误信息'}"
        md_path = find_markdown_output(output_dir, markdown_path)
        if not md_path:
            message = read_log_tail(stderr_path) or read_log_tail(stdout_path)
            return "failed", None, f"MinerU 未生成 Markdown 文件。{message}"
        review_reason = markdown_needs_review(md_path)
        return ("need_review" if review_reason else "success"), md_path, review_reason

    def _parse_with_official_api(self, record: PaperRecord, pdf_path: Path, output_dir: Path, markdown_path: Path, task: ParseTask | None = None) -> tuple[str, Path | None, str | None]:
        if not mineru_api_token():
            return "failed", None, "未配置 MINERU_API_TOKEN，请先在 .env 中填写 MinerU API Token"
        timeout = int(self.config.get("timeout_seconds") or 7200)
        poll_interval = max(2, int(self.config.get("api_poll_interval_seconds") or 5))
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._update_task(task, 2, "申请 MinerU 官方 API 上传地址")
            pdf_body = pdf_path.read_bytes()
            upload_info = self._official_api_create_batch(record, pdf_path)
            upload_url = upload_info["upload_url"]
            batch_id = upload_info["batch_id"]
            self._update_task(task, 8, "上传 PDF 到 MinerU 官方 API")
            self._official_api_upload_pdf(upload_url, pdf_body)
            self._update_task(task, 20, "等待 MinerU 云端解析结果")
            result = self._official_api_wait_for_result(batch_id, timeout, poll_interval, task)
            self._update_task(task, 92, "下载 MinerU Markdown 结果")
            markdown = self._official_api_result_markdown(result)
            markdown_path.write_text(markdown, encoding="utf-8")
        except Exception as exc:
            return "failed", None, f"MinerU 官方 API 解析失败：{exc}"
        review_reason = markdown_needs_review(markdown_path)
        return ("need_review" if review_reason else "success"), markdown_path, review_reason

    def _official_api_create_batch(self, record: PaperRecord, pdf_path: Path) -> dict[str, str]:
        filename = safe_filename(pdf_path.name or f"{record.paper_id}.pdf", 120)
        payload = {
            "enable_formula": config_bool(self.config.get("api_enable_formula"), True),
            "enable_table": config_bool(self.config.get("api_enable_table"), True),
            "language": clean_text(str(self.config.get("api_language") or "en")),
            "files": [
                {
                    "name": filename,
                    "is_ocr": config_bool(self.config.get("api_is_ocr"), False),
                    "data_id": record.paper_id,
                }
            ],
        }
        model_version = clean_text(str(self.config.get("api_model_version") or "vlm"))
        if model_version:
            payload["model_version"] = model_version
        extra_formats = self.config.get("api_extra_formats")
        if isinstance(extra_formats, list) and extra_formats:
            payload["extra_formats"] = extra_formats
        result = mineru_api_json_request(self.config, "POST", "/api/v4/file-urls/batch", payload, timeout=60)
        data = mineru_api_payload_data(result)
        if not isinstance(data, dict):
            raise RuntimeError("申请上传地址失败：MinerU API 未返回 data")
        batch_id = clean_text(str(first_present(data, ["batch_id", "batchId", "id"]) or ""))
        raw_urls = first_present(data, ["file_urls", "fileUrls", "urls", "upload_urls", "uploadUrls"])
        upload_url = ""
        if isinstance(raw_urls, list) and raw_urls:
            first = raw_urls[0]
            upload_url = clean_text(str(first.get("url") if isinstance(first, dict) else first))
        elif isinstance(raw_urls, dict):
            upload_url = clean_text(str(first_present(raw_urls, ["url", "upload_url", "uploadUrl"]) or ""))
        if not batch_id:
            raise RuntimeError(f"申请上传地址失败：缺少 batch_id，返回 {data}")
        if not upload_url:
            raise RuntimeError(f"申请上传地址失败：缺少 file_urls，返回 {data}")
        return {"batch_id": batch_id, "upload_url": upload_url}

    def _official_api_upload_pdf(self, upload_url: str, pdf_body: bytes) -> None:
        attempts = max(1, int(self.config.get("api_upload_retries") or 3))
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                mineru_oss_put_bytes(upload_url, pdf_body)
                return
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError, http.client.HTTPException) as exc:
                last_error = exc
                if is_ssl_eof_error(exc):
                    log_event("MinerU API upload ended with SSL EOF; continuing to result polling", {"attempt": attempt})
                    return
                if attempt < attempts:
                    time.sleep(min(2 * attempt, 6))
                    continue
        raise RuntimeError(f"上传 PDF 到 MinerU 失败：{last_error}")

    def _official_api_wait_for_result(self, batch_id: str, timeout: int, poll_interval: int, task: ParseTask | None = None) -> dict[str, Any]:
        deadline = time.time() + timeout
        started = time.time()
        last_result: dict[str, Any] = {}
        while time.time() < deadline:
            elapsed = max(0.0, time.time() - started)
            progress = min(90.0, 20.0 + elapsed / max(60.0, timeout) * 70.0)
            self._update_task(task, progress, "等待 MinerU 云端解析结果")
            try:
                result = mineru_api_json_request(self.config, "GET", f"/api/v4/extract-results/batch/{urllib.parse.quote(batch_id)}", timeout=60)
            except RuntimeError as exc:
                if is_ssl_eof_error(exc):
                    self._update_task(task, progress, "等待 MinerU 云端解析结果（网络重试中）", str(exc)[:240])
                    time.sleep(poll_interval)
                    continue
                raise
            data = mineru_api_payload_data(result)
            last_result = data if isinstance(data, dict) else result
            status, message = self._official_api_status(last_result)
            if status in {"done", "success"}:
                return last_result
            if status in {"failed", "error"}:
                raise RuntimeError(message or f"任务 {batch_id} 解析失败")
            time.sleep(poll_interval)
        raise RuntimeError(f"任务 {batch_id} 等待超时，最后状态：{last_result}")

    def _official_api_status(self, result: dict[str, Any]) -> tuple[str, str]:
        candidates: list[dict[str, Any]] = []
        for key in ["extract_result", "extractResult", "results", "files", "tasks"]:
            value = result.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
        if isinstance(result.get("file"), dict):
            candidates.append(result["file"])
        candidates.append(result)
        statuses = [clean_text(str(first_present(item, ["state", "status", "extract_status", "extractStatus"]) or "")).lower() for item in candidates]
        message = clean_text(str(first_present(candidates[0], ["err_msg", "error", "message", "msg"]) or "")) if candidates else ""
        if any(status in {"failed", "fail", "error"} for status in statuses):
            return "failed", message
        if any(status in {"done", "success", "completed", "finish", "finished"} for status in statuses):
            return "success", message
        return "running", message

    def _official_api_result_markdown(self, result: dict[str, Any]) -> str:
        candidates: list[Any] = [result]
        for key in ["extract_result", "extractResult", "results", "files", "tasks"]:
            value = result.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            markdown = first_present(item, ["markdown", "md", "content"])
            if isinstance(markdown, str) and markdown.strip():
                return markdown
            md_url = first_present(item, ["md_url", "mdUrl", "markdown_url", "markdownUrl", "full_md_url", "fullMdUrl"])
            if isinstance(md_url, str) and md_url.strip():
                return mineru_download_bytes(md_url).decode("utf-8", errors="replace")
            zip_url = first_present(item, ["full_zip_url", "fullZipUrl", "zip_url", "zipUrl", "result_url", "resultUrl"])
            if isinstance(zip_url, str) and zip_url.strip():
                data = mineru_download_bytes(zip_url)
                if data[:2] == b"PK":
                    return extract_markdown_from_zip_bytes(data)
                return data.decode("utf-8", errors="replace")
        raise RuntimeError(f"MinerU API 结果中未找到 Markdown 或结果下载链接：{result}")

    def _parse_with_service(self, record: PaperRecord, pdf_path: Path, output_dir: Path, markdown_path: Path) -> tuple[str, Path | None, str | None]:
        service_url = clean_text(str(self.config.get("service_url") or ""))
        if not service_url:
            return "need_review", None, "未配置 MinerU 外部服务地址 mineru_adapter.service_url"
        payload = {
            "paper_id": record.paper_id,
            "title": record.title,
            "pdf_path": str(pdf_path),
            "output_dir": str(output_dir),
            "markdown_path": str(markdown_path),
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(service_url, data=data, headers={"Content-Type": "application/json; charset=utf-8"})
        timeout = int(self.config.get("timeout_seconds") or 600)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            return "failed", None, f"MinerU 服务返回 HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return "failed", None, f"MinerU 服务调用失败：{exc}"
        if not isinstance(result, dict) or result.get("ok") is False:
            return "failed", None, clean_text(str((result or {}).get("error") or "MinerU 服务未返回成功状态"))
        if result.get("markdown"):
            markdown_path.write_text(str(result["markdown"]), encoding="utf-8")
        elif result.get("markdown_path"):
            source_path = Path(str(result["markdown_path"]))
            if source_path.exists():
                if source_path.resolve() != markdown_path.resolve():
                    markdown_path.write_text(source_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        md_path = find_markdown_output(output_dir, markdown_path)
        if not md_path:
            return "failed", None, "MinerU 服务未生成 Markdown 文件"
        review_reason = clean_text(str(result.get("review_reason") or "")) or markdown_needs_review(md_path)
        parse_status = clean_text(str(result.get("parse_status") or "")).lower()
        if parse_status in {"success", "failed", "need_review"}:
            return parse_status, md_path if parse_status != "failed" else None, review_reason or clean_text(str(result.get("error") or ""))
        return ("need_review" if review_reason else "success"), md_path, review_reason


def update_record_after_parse(record: PaperRecord, parse_status: str, markdown_path: Path | None, message: str | None) -> PaperRecord:
    records = load_paper_records()
    current = records.get(record.paper_id, record)
    current.parse_status = parse_status
    current.parse_error = message if parse_status != "success" else None
    current.parse_time = datetime.now().isoformat(timespec="seconds")
    current.parser = "mineru_adapter"
    if markdown_path and parse_status in {"success", "need_review"}:
        current.markdown_path = relative_path(markdown_path)
    records[current.paper_id] = current
    save_paper_records(records)
    return current


def mark_record_parse_running(record: PaperRecord, task: ParseTask) -> PaperRecord:
    records = load_paper_records()
    current = records.get(record.paper_id, record)
    current.parse_status = task.parse_status
    current.parse_error = task.message
    current.parse_time = datetime.now().isoformat(timespec="seconds")
    current.parser = "mineru_adapter"
    records[current.paper_id] = current
    save_paper_records(records)
    return current


def run_parse_task(task_id: str) -> int:
    task = load_parse_task(task_id)
    if not task:
        return 1
    records = load_paper_records()
    record = records.get(task.paper_id)
    if not record:
        task.status = "failed"
        task.parse_status = "failed"
        task.message = "未找到论文获取记录"
        task.completed_at = datetime.now().isoformat(timespec="seconds")
        save_parse_task(task)
        return 1
    if not acquire_parse_lock(task):
        return 1
    task.status = "running"
    task.parse_status = "running"
    task.started_at = datetime.now().isoformat(timespec="seconds")
    task.message = "MinerU 解析中"
    task.progress_percent = 1
    adapter_mode = clean_text(str((CONFIG.get("mineru_adapter") or {}).get("mode") or "command")).lower()
    task.progress_stage = "提交 MinerU 官方 API" if adapter_mode in {"official_api", "api", "mineru_api"} else "准备 MinerU 模型"
    record = mark_record_parse_running(record, task)
    task.record = asdict(record)
    save_parse_task(task)
    try:
        parse_status, markdown_path, message = MinerUAdapter(CONFIG.get("mineru_adapter", {})).parse(record, task)
        updated = update_record_after_parse(record, parse_status, markdown_path, message)
        task.parse_status = parse_status
        task.status = "success" if parse_status == "success" else parse_status
        task.markdown_path = updated.markdown_path
        task.message = message or ("解析成功" if parse_status == "success" else "解析完成，需人工复核")
        task.record = asdict(updated)
        task.progress_percent = 100 if parse_status in {"success", "need_review"} else task.progress_percent
        task.progress_stage = "解析完成" if parse_status in {"success", "need_review"} else "解析失败"
        task.completed_at = datetime.now().isoformat(timespec="seconds")
        save_parse_task(task)
        log_event("paper parse task completed", {"task_id": task_id, "paper_id": record.paper_id, "parse_status": parse_status, "message": task.message})
        return 0 if parse_status in {"success", "need_review"} else 1
    finally:
        release_parse_lock(task_id)


def start_parse_task_for_record(record: PaperRecord) -> ParseTask:
    ensure_dirs()
    active_task = latest_parse_task_for_paper(record.paper_id, {"queued", "running"})
    if active_task:
        updated_record = mark_record_parse_running(record, active_task)
        active_task.record = asdict(updated_record)
        save_parse_task(active_task)
        return active_task
    existing_md = resolve_record_markdown_path(record)
    if record.parse_status == "success" and existing_md:
        task = ParseTask(
            task_id="task_" + uuid.uuid4().hex[:18],
            paper_id=record.paper_id,
            status="success",
            completed_at=datetime.now().isoformat(timespec="seconds"),
            message="Markdown 已存在，复用解析结果",
            markdown_path=record.markdown_path,
            parse_status="success",
            record=asdict(record),
        )
        save_parse_task(task)
        return task
    task = ParseTask(
        task_id="task_" + uuid.uuid4().hex[:18],
        paper_id=record.paper_id,
        status="queued",
        message="MinerU 解析任务已排队",
        parse_status="queued",
        progress_percent=0,
        progress_stage="排队等待",
    )
    if record.fetch_status != "success" or not resolve_record_pdf_path(record):
        task.status = "failed"
        task.parse_status = "failed"
        task.message = "PDF 不存在，无法解析；请先成功获取 PDF"
        task.completed_at = datetime.now().isoformat(timespec="seconds")
        updated = update_record_after_parse(record, "failed", None, task.message)
        task.record = asdict(updated)
        save_parse_task(task)
        return task
    updated_record = mark_record_parse_running(record, task)
    task.record = asdict(updated_record)
    save_parse_task(task)
    command = [sys.executable, str(Path(__file__).resolve()), "--run-parse-task", task.task_id]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        task.worker_pid = process.pid
        save_parse_task(task)
    except OSError as exc:
        task.status = "failed"
        task.parse_status = "failed"
        task.message = f"解析子进程启动失败：{exc}"
        task.completed_at = datetime.now().isoformat(timespec="seconds")
        updated = update_record_after_parse(record, "failed", None, task.message)
        task.record = asdict(updated)
        save_parse_task(task)
    return task


def deepseek_single_paper_analysis(topic: str, paper: Paper) -> dict[str, Any]:
    full_text, text_source, _paper_id = paper_text_source(paper)
    science_record = science_record_for_paper(paper)
    fallback = {
        "ok": True,
        "provider": "local_fallback",
        "analysis": fallback_paper_chinese_summary(topic, paper),
        "text_source": text_source,
        "science_record": asdict(science_record),
    }
    if not CONFIG.get("deepseek_enabled", True) or not os.getenv("DEEPSEEK_API_KEY"):
        return fallback
    key_material = json.dumps(
        {
            "topic": topic,
            "paper": asdict(paper),
            "mode": "single_paper_detail_v2",
            "text_source": text_source,
            "text_hash": hashlib.sha1(full_text[:80000].encode("utf-8", errors="ignore")).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cached = read_cache("deepseek_single_paper", key_material, max_age_hours=24 * 30)
    if isinstance(cached, dict):
        return cached
    analysis_text = full_text[:60000] if text_source == "markdown" else paper.abstract
    prompt = {
        "topic": topic,
        "paper": {
            "title": paper.title,
            "year": paper.year,
            "authors": paper.authors[:12],
            "source": paper.source,
            "url": paper.url,
            "abstract": paper.abstract,
            "text_source": text_source,
            "full_text_or_abstract": analysis_text,
            "light_science_record": asdict(science_record),
        },
        "requirements": [
            "请优先基于 full_text_or_abstract 字段生成中文详细分析；当 text_source=markdown 时，它来自 MinerU 生成的全文 Markdown。",
            "不要编造原文中没有的信息；未在全文中出现的物理参数不要补充。",
            "说明研究问题、数据/方法、主要发现或贡献、局限性、与检索话题的关系。",
            "如果能辅助天文观测目标筛选，请指出涉及的恒星/宿主星/天体名称和筛选价值，并参考 light_science_record 中的证据。",
            "输出 JSON 对象。",
        ],
        "output_schema": {
            "analysis": "500-900字中文详细分析",
            "key_points": ["3-6条要点"],
            "target_relevance": "对后续观测目标筛选的价值",
            "mentioned_targets": ["文中涉及的恒星、宿主星或天体名称"],
        },
    }
    try:
        parsed = deepseek_chat_json(
            [
                {"role": "system", "content": "你是严谨的天文学文献分析助手。只输出可解析 JSON。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            timeout=100,
        )
        if not isinstance(parsed, dict):
            raise ValueError("single paper response is not a JSON object")
        result = {
            "ok": True,
            "provider": "deepseek",
            "analysis": clean_text(str(parsed.get("analysis", ""))),
            "key_points": [clean_text(str(item)) for item in parsed.get("key_points", [])][:8],
            "target_relevance": clean_text(str(parsed.get("target_relevance", ""))),
            "mentioned_targets": [clean_text(str(item)) for item in parsed.get("mentioned_targets", [])][:20],
            "text_source": text_source,
            "science_record": asdict(science_record),
        }
        if not result["analysis"]:
            return fallback
        write_cache("deepseek_single_paper", key_material, result)
        return result
    except Exception as exc:
        log_event("DeepSeek single paper analysis failed; falling back to local summary", {"topic": topic, "title": paper.title, "error": str(exc)})
        return fallback


def serialize_paper_for_topic(topic: str, paper: Paper, summary_map: dict[str, str] | None = None) -> dict[str, Any]:
    data = asdict(paper)
    data["paper_id"] = paper_id_for(paper)
    record = paper_record_payload(paper)
    if record:
        data["paper_record"] = record
        data["fetch_status"] = record.get("fetch_status")
        data["pdf_path"] = record.get("pdf_path")
        data["markdown_path"] = record.get("markdown_path")
    science = load_science_records().get(data["paper_id"])
    if science:
        data["science_record"] = asdict(science)
        data["science_source"] = science.source
    return data

def target_mentions_from_papers(target: Target, papers: list[Paper]) -> list[Paper]:
    aliases = [alias.lower() for alias in ([target.name or "", f"TIC {target.id}"] + target.aliases) if alias and len(alias.strip()) >= 3]
    matches: list[Paper] = []
    for paper in papers:
        text = f"{paper.title} {paper.abstract}".lower()
        if any(alias in text for alias in aliases):
            matches.append(paper)
    return matches


def make_target_id(name: str) -> str:
    cleaned = re.sub(r"\s+", "_", name.strip())
    cleaned = re.sub(r"[^0-9A-Za-z_.+-]", "", cleaned)
    return cleaned or hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


def normalize_stellar_target_name(name: str) -> str:
    value = re.sub(r"\s+", " ", name.strip())
    replacements = [
        (r"\b(TRAPPIST[-\s]?1)[a-h]\b", r"\1"),
        (r"\b(GJ\s*\d+)[a-z]\b", r"\1"),
        (r"\b(Gliese\s*\d+)[a-z]\b", r"\1"),
        (r"\b(Kepler[-\s]?\d+)[a-z]\b", r"\1"),
        (r"\b(K2[-\s]?\d+)[a-z]\b", r"\1"),
        (r"\b(TOI[-\s]?\d+(?:\.\d+)?)[a-z]\b", r"\1"),
    ]
    for pattern, repl in replacements:
        value = re.sub(pattern, repl, value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def is_likely_planet_name(original: str, normalized: str) -> bool:
    return original.strip().lower() != normalized.strip().lower()


def extract_observation_targets(topic: str, papers: list[Paper]) -> list[dict[str, Any]]:
    patterns = [
        r"\bTIC\s*\d+\b",
        r"\bTOI[-\s]?\d+(?:\.\d+)?[a-z]?\b",
        r"\bHD\s*\d+[A-Z]?\b",
        r"\bHIP\s*\d+\b",
        r"\bGJ\s*\d+[A-Za-z]?\b",
        r"\bGliese\s*\d+[A-Za-z]?\b",
        r"\bK2[-\s]?\d+[A-Za-z]?\b",
        r"\bKepler[-\s]?\d+[a-z]?\b",
        r"\bTRAPPIST[-\s]?1[a-z]?\b",
        r"\bProxima\s+Centauri\b",
        r"\bAlpha\s+Centauri\b",
        r"\bTau\s+Ceti\b",
        r"\bLHS\s*\d+\b",
        r"\bWolf\s*\d+\b",
    ]
    candidates: dict[str, dict[str, Any]] = {}
    target_catalog = load_targets()
    alias_lookup: list[tuple[str, Target]] = []
    for target in target_catalog:
        for alias in [target.name or "", f"TIC {target.id}", *target.aliases]:
            alias = alias.strip()
            if len(alias) >= 4:
                alias_lookup.append((alias, target))
    for paper in papers:
        full_text, text_source, _resolved_paper_id = paper_text_source(paper, max_chars=90000)
        text = full_text or f"{paper.title} {paper.abstract}"
        science = science_record_for_paper(paper)
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                raw_name = re.sub(r"\s+", " ", match.group(0)).strip()
                name = normalize_stellar_target_name(raw_name)
                key = name.lower()
                item = candidates.setdefault(
                    key,
                    {
                        "id": make_target_id(name),
                        "name": name,
                        "source_name": raw_name,
                        "matched_catalog_id": None,
                        "papers": [],
                        "paper_keys": set(),
                        "mention_count": 0,
                        "normalized_from_planet": is_likely_planet_name(raw_name, name),
                        "evidence": [],
                    },
                )
                item["mention_count"] += len(re.findall(pattern, text, flags=re.IGNORECASE))
                paper_key = paper.url or paper.title
                if paper_key not in item["paper_keys"]:
                    item["paper_keys"].add(paper_key)
                    item["papers"].append(paper)
                if text_source == "markdown" and len(item["evidence"]) < 5:
                    matched_target = next((target for target in science.targets if target.get("name", "").lower() == key), None)
                    if matched_target:
                        for ref_index in matched_target.get("evidence_refs", [])[:2]:
                            if 0 <= int(ref_index) < len(science.evidence):
                                item["evidence"].append(science.evidence[int(ref_index)])
        lower_text = text.lower()
        for alias, target in alias_lookup:
            if alias.lower() in lower_text:
                key = (target.name or alias).lower()
                item = candidates.setdefault(
                    key,
                    {
                        "id": target.id,
                        "name": target.name or alias,
                        "source_name": alias,
                        "matched_catalog_id": target.id,
                        "papers": [],
                        "paper_keys": set(),
                        "mention_count": 0,
                        "evidence": [],
                    },
                )
                item["mention_count"] += lower_text.count(alias.lower())
                paper_key = paper.url or paper.title
                if paper_key not in item["paper_keys"]:
                    item["paper_keys"].add(paper_key)
                    item["papers"].append(paper)
                if text_source == "markdown" and len(item["evidence"]) < 5:
                    matched_target = next((target_item for target_item in science.targets if alias in target_item.get("aliases", [])), None)
                    if matched_target:
                        for ref_index in matched_target.get("evidence_refs", [])[:2]:
                            if 0 <= int(ref_index) < len(science.evidence):
                                item["evidence"].append(science.evidence[int(ref_index)])
    result = []
    for item in candidates.values():
        item["papers"] = dedupe_papers(item["papers"])
        item["related_paper_count"] = len(item["papers"])
        item.pop("paper_keys", None)
        item["evidence"] = item.get("evidence", [])[:8]
        result.append(item)
    result.sort(key=lambda item: (len(item["papers"]), item["mention_count"]), reverse=True)
    return result


def paper_rank_key(paper: Paper) -> str:
    return re.sub(r"\W+", "", paper.title.lower())[:80] or paper.url


def rank_weights() -> dict[str, float]:
    defaults = {"semantic": 0.35, "target": 0.30, "evidence": 0.20, "recency": 0.15}
    configured = CONFIG.get("target_search_rank_weights", {})
    if isinstance(configured, dict):
        defaults.update({key: float(value) for key, value in configured.items() if key in defaults})
    return defaults


def science_record_for_scoring(paper: Paper) -> ScienceRecord:
    record = paper_record_for_paper(paper)
    if record and resolve_record_markdown_path(record):
        return science_record_for_paper(paper)
    text = "\n\n".join([paper.title, paper.abstract])
    return extract_science_from_text(paper_id_for(paper), text, "abstract")


def evidence_signal_for_paper(paper: Paper, context: dict[str, Any]) -> tuple[float, list[str]]:
    try:
        science = science_record_for_scoring(paper)
    except Exception as exc:
        log_event("N4 science evidence scoring failed", {"title": paper.title, "error": str(exc)})
        return 0.0, []
    if not science.evidence and not science.targets:
        return 0.0, []
    signal = 0.25 if science.evidence else 0.1
    reasons: list[str] = []
    if science.source == "markdown" and science.evidence:
        signal = max(signal, min(1.0, len(science.evidence) / 4))
        reasons.append(f"MinerU全文证据 {len(science.evidence)} 条")
    context_aliases = {
        alias.lower()
        for entry in context.get("targets", [])
        for alias in [*(entry.get("aliases") or []), *(entry.get("matched_aliases") or [])]
    }
    for item in science.targets:
        target_aliases = {str(item.get("name", "")).lower(), *(str(alias).lower() for alias in item.get("aliases", []))}
        if context_aliases and target_aliases.intersection(context_aliases):
            signal = max(signal, 0.9 if science.source == "markdown" else 0.55)
            reasons.append(f"科学信息命中 {item.get('name')}")
            break
    return min(1.0, signal), reasons[:2]


def recency_signal_for_paper(paper: Paper, timeframe_months: int | None = None) -> tuple[float, str]:
    value = paper_date(paper)
    if not value:
        return 0.25, "年份未知"
    days = (date.today() - value).days
    if days <= 365:
        return 1.0, "近一年文献"
    if days <= 730:
        return 0.75, "近两年文献"
    cutoff = cutoff_for_months(timeframe_months)
    if cutoff and value >= cutoff:
        return 0.55, "处于所选时间范围"
    return 0.2, f"{value.year} 年文献"


def target_signal_for_paper(paper: Paper, context: dict[str, Any], text: str) -> tuple[float, list[str], dict[str, Any] | None]:
    if not context.get("targets"):
        return 0.0, [], None
    best_signal = 0.0
    best_reasons: list[str] = []
    best_target: dict[str, Any] | None = None
    for entry in context.get("targets", []):
        target_ref = entry.get("target") or {}
        direct_aliases = entry.get("aliases") or []
        direct_hits = [alias for alias in direct_aliases if len(str(alias)) >= 3 and entity_term_matches(text, str(alias))]
        signal = 0.0
        reasons: list[str] = []
        if paper.target_id and str(paper.target_id) == str(target_ref.get("id")):
            signal = max(signal, 0.75)
            reasons.append(f"目标检索 {target_ref.get('name') or target_ref.get('id')}")
        if direct_hits:
            signal = max(signal, 1.0)
            reasons.append(f"命中别名 {direct_hits[0]}")
        for related in entry.get("related_targets") or []:
            aliases = related.get("aliases") or []
            related_hits = [alias for alias in aliases if len(str(alias)) >= 3 and entity_term_matches(text, str(alias))]
            if related_hits:
                signal = max(signal, 0.65)
                distance = related.get("distance_deg")
                if distance is not None:
                    reasons.append(f"邻近天区 {related.get('name') or related.get('id')} ({float(distance):.2f} deg)")
                else:
                    reasons.append(f"扩展关系 {related.get('name') or related.get('id')}")
                break
        if signal > best_signal:
            best_signal = signal
            best_reasons = reasons
            best_target = target_ref
    return best_signal, best_reasons[:2], best_target


def score_paper_for_target_search(topic: str, paper: Paper, context: dict[str, Any], timeframe_months: int | None = None) -> dict[str, Any]:
    text, _source, _resolved = paper_text_source(paper, max_chars=90000)
    text = text or f"{paper.title} {paper.abstract}"
    semantic_signal = max(float(paper.relevance or 0.0), topic_relevance(topic, paper.title, paper.abstract))
    target_signal, target_reasons, matched_target = target_signal_for_paper(paper, context, text)
    evidence_signal, evidence_reasons = evidence_signal_for_paper(paper, context)
    recency_signal, recency_reason = recency_signal_for_paper(paper, timeframe_months)
    weights = rank_weights()
    total_weight = sum(weights.values()) or 1.0
    score = (
        semantic_signal * weights["semantic"]
        + target_signal * weights["target"]
        + evidence_signal * weights["evidence"]
        + recency_signal * weights["recency"]
    ) / total_weight
    reasons = []
    if semantic_signal >= 0.35:
        reasons.append(f"语义相关度 {semantic_signal:.2f}")
    reasons.extend(target_reasons)
    reasons.extend(evidence_reasons)
    reasons.append(recency_reason)
    return {
        "rank_score": round(min(100.0, score * 100), 1),
        "rank_reasons": reasons[:5],
        "matched_target": matched_target,
        "score_breakdown": {
            "semantic_relevance": round(semantic_signal * 35, 1),
            "target_region_relevance": round(target_signal * 30, 1),
            "full_text_evidence": round(evidence_signal * 20, 1),
            "recent_trend": round(recency_signal * 15, 1),
        },
    }


def rerank_papers_for_target_search(topic: str, papers: list[Paper], context: dict[str, Any], timeframe_months: int | None = None) -> tuple[list[Paper], dict[str, dict[str, Any]]]:
    rank_map: dict[str, dict[str, Any]] = {}
    for paper in papers:
        rank_map[paper_rank_key(paper)] = score_paper_for_target_search(topic, paper, context, timeframe_months)
    ordered = sorted(papers, key=lambda item: rank_map.get(paper_rank_key(item), {}).get("rank_score", 0), reverse=True)
    return ordered, rank_map


def serialize_ranked_paper(topic: str, paper: Paper, rank_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    data = serialize_paper_for_topic(topic, paper)
    rank_info = rank_map.get(paper_rank_key(paper), {})
    data.update(rank_info)
    return data


def score_breakdown_average(rank_infos: list[dict[str, Any]]) -> dict[str, float]:
    if not rank_infos:
        return {}
    keys = ["semantic_relevance", "target_region_relevance", "full_text_evidence", "recent_trend"]
    return {
        key: round(sum(float(info.get("score_breakdown", {}).get(key, 0)) for info in rank_infos) / len(rank_infos), 1)
        for key in keys
    }


def compact_target_search_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": context.get("query", {}),
        "strict_candidates": bool(CONFIG.get("target_search_strict_candidates", True)),
        "filtered_candidate_count": int(context.get("filtered_candidate_count") or 0),
        "targets": [
            {
                "target": entry.get("target"),
                "matched_aliases": entry.get("matched_aliases", []),
                "aliases": entry.get("aliases", [])[: int(CONFIG.get("target_search_alias_limit", 10))],
                "related_targets": entry.get("related_targets", []),
            }
            for entry in context.get("targets", [])
        ],
    }


def expansion_for_candidate(candidate: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    candidate_id = str(candidate.get("matched_catalog_id") or candidate.get("id") or "")
    candidate_name = str(candidate.get("name") or "").lower()
    for entry in context.get("targets", []):
        target_ref = entry.get("target") or {}
        target_aliases = {str(alias).lower() for alias in entry.get("aliases", [])}
        if candidate_id and candidate_id == str(target_ref.get("id")):
            return entry
        if candidate_name and (candidate_name == str(target_ref.get("name") or "").lower() or candidate_name in target_aliases):
            return entry
        for related in entry.get("related_targets", []):
            related_aliases = {str(alias).lower() for alias in related.get("aliases", [])}
            if candidate_id and candidate_id == str(related.get("id")):
                return {"target": related, "aliases": related.get("aliases", []), "matched_aliases": [], "related_targets": [], "source_expansion": entry.get("target")}
            if candidate_name and (candidate_name == str(related.get("name") or "").lower() or candidate_name in related_aliases):
                return {"target": related, "aliases": related.get("aliases", []), "matched_aliases": [], "related_targets": [], "source_expansion": entry.get("target")}
    return None


def constrain_extracted_targets_for_context(extracted_targets: list[dict[str, Any]], context: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    if not CONFIG.get("target_search_strict_candidates", True) or not context.get("targets"):
        return extracted_targets, 0
    kept: list[dict[str, Any]] = []
    filtered = 0
    for item in extracted_targets:
        expansion = expansion_for_candidate(item, context)
        if expansion:
            item["target_expansion"] = expansion
            kept.append(item)
        else:
            filtered += 1
    return kept, filtered


def validate_extracted_hotspots(hotspots: list[dict[str, Any]]) -> dict[str, Any]:
    duplicate_ids = []
    seen = set()
    missing_fields = 0
    for item in hotspots:
        if "id" not in item or "heat" not in item:
            missing_fields += 1
        item_id = str(item.get("id", ""))
        if item_id in seen:
            duplicate_ids.append(item_id)
        seen.add(item_id)
    return {
        "valid": missing_fields == 0 and not duplicate_ids,
        "target_count": len(hotspots),
        "hotspot_count": len(hotspots),
        "matched_count": len(hotspots),
        "unmatched_ids": [],
        "duplicate_ids": duplicate_ids,
        "missing_fields_count": missing_fields,
    }


def build_topic_hotspots(topic: str, limit: int | None = None, timeframe_months: int | None = None, max_papers: int | None = None) -> dict[str, Any]:
    ensure_dirs()
    timeframe_months = int(timeframe_months or CONFIG.get("default_timeframe_months", 12))
    if max_papers is None:
        max_papers = int(CONFIG.get("max_topic_papers", 200))
    target_context = build_target_search_context(topic)
    papers = collect_topic_papers(topic, timeframe_months, limit=max_papers, target_context=target_context)
    papers, paper_rank_map = rerank_papers_for_target_search(topic, papers, target_context, timeframe_months)
    topic_summary = summarize_topic(topic, papers)
    corpus_path = write_literature_corpus(topic, papers, timeframe_months)
    results: list[dict[str, Any]] = []
    if not papers:
        output_path = config_path("output_file", "outputs/hotspots.json")
        try_write_text(output_path, "[]", encoding="utf-8")
        output = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "topic",
            "topic": topic,
            "search_terms": topic_search_terms(topic),
            "target_search": compact_target_search_context(target_context),
            "timeframe_months": timeframe_months,
            "topic_summary": topic_summary,
            "corpus_file": str(corpus_path),
            "papers": [],
            "items": [],
            "validation": validate_extracted_hotspots([]),
        }
        log_event("topic analysis completed with no papers", {"topic": topic})
        return output
    extracted_targets = extract_observation_targets(topic, papers)
    extracted_targets, filtered_candidate_count = constrain_extracted_targets_for_context(extracted_targets, target_context)
    if filtered_candidate_count:
        target_context["filtered_candidate_count"] = filtered_candidate_count
    llm_overview = deepseek_topic_overview(topic, papers, extracted_targets)
    if llm_overview:
        topic_summary = {
            **topic_summary,
            "summary": llm_overview.get("summary") or topic_summary.get("summary"),
            "focus_points": llm_overview.get("focus_points") or topic_summary.get("focus_points", []),
            "hotspot_overview": llm_overview.get("hotspot_overview", ""),
            "target_ranking_notes": llm_overview.get("target_ranking_notes", []),
            "representative_papers": llm_overview.get("representative_papers", []),
            "analysis_provider": "deepseek",
        }
    else:
        topic_summary = {**topic_summary, "analysis_provider": "local_fallback"}
    for extracted in extracted_targets:
        matched = extracted["papers"]
        matched = sorted(matched, key=lambda paper: paper_rank_map.get(paper_rank_key(paper), {}).get("rank_score", 0), reverse=True)
        matched_rank_infos = [paper_rank_map.get(paper_rank_key(paper), {}) for paper in matched]
        top_rank = max([float(info.get("rank_score", 0)) for info in matched_rank_infos] or [0.0])
        avg_rank = sum(float(info.get("rank_score", 0)) for info in matched_rank_infos[:3]) / max(1, min(3, len(matched_rank_infos)))
        pseudo_target = Target(id=str(extracted["id"]), name=extracted["name"])
        analysis = heuristic_analyze(pseudo_target, matched)
        score = score_target(pseudo_target, matched, analysis, timeframe_months)
        expansion = extracted.get("target_expansion") or expansion_for_candidate(extracted, target_context)
        rank_score = round(min(100.0, max(score["heat"], top_rank * 0.65 + avg_rank * 0.35)), 1)
        reasons = []
        if expansion:
            matched_aliases = expansion.get("matched_aliases") or []
            if matched_aliases:
                reasons.append(f"检索扩展命中 {matched_aliases[0]}")
            related_count = len(expansion.get("related_targets") or [])
            if related_count:
                reasons.append(f"扩展相关目标 {related_count} 个")
        if any(info.get("rank_reasons") for info in matched_rank_infos):
            for reason in matched_rank_infos[0].get("rank_reasons", []):
                if reason not in reasons:
                    reasons.append(reason)
        if extracted.get("evidence"):
            reasons.append("全文证据命中")
        reasons.append(f"提及 {extracted.get('mention_count', len(matched))} 次")
        score_breakdown = {
            **score["score_breakdown"],
            **score_breakdown_average(matched_rank_infos[:5]),
        }
        results.append(
            {
                "id": str(extracted["id"]),
                "name": extracted["name"],
                "query": target_context.get("query", {}),
                "target": expansion.get("target") if expansion else {"id": str(extracted["id"]), "name": extracted["name"]},
                "aliases": expansion.get("aliases", []) if expansion else [],
                "related_targets": expansion.get("related_targets", []) if expansion else [],
                "matched_catalog_id": extracted.get("matched_catalog_id"),
                "reference_catalog_id": extracted.get("matched_catalog_id"),
                "in_reference_catalog": bool(extracted.get("matched_catalog_id")),
                "mention_count": extracted.get("mention_count", len(matched)),
                "related_paper_count": extracted.get("related_paper_count", len(matched)),
                "rank_score": rank_score,
                "heat": rank_score,
                "matched": True,
                "evidence": extracted.get("evidence", [])[:8],
                "match_reasons": reasons[:6],
                "reasons": reasons[:6],
                "papers": [
                    serialize_ranked_paper(topic, paper, paper_rank_map)
                    for paper in matched[:10]
                ],
                "summary": (
                    "\u76ee\u6807 {name} \u5728\u201c{topic}\u201d\u76f8\u5173\u6587\u732e\u4e2d\u88ab\u63d0\u53ca\u3002"
                    "\u76f8\u5173\u6587\u732e\u6570\u4e3a {paper_count} \u7bc7\uff1b"
                    "\u6587\u672c\u63d0\u53ca\u6b21\u6570\u4e3a {mention_count} \u6b21\u3002"
                    "\u70ed\u5ea6\u8bc4\u5206\u4e3b\u8981\u4f9d\u636e\u53bb\u91cd\u540e\u7684\u76f8\u5173\u6587\u732e\u6570\u3001\u8fd1\u671f\u7a0b\u5ea6\u3001\u76f8\u5173\u6027\u548c\u4ee3\u8868\u6027\u6587\u732e\u8ba1\u7b97\u3002"
                ).format(
                    name=extracted["name"],
                    topic=topic,
                    paper_count=extracted.get("related_paper_count", len(matched)),
                    mention_count=extracted.get("mention_count", len(matched)),
                ),
                "topics": topic_summary["focus_points"],
                "analysis_provider": "literature-target-extraction",
                "updated_at": date.today().isoformat(),
                "timeframe_months": timeframe_months,
                "score_breakdown": score_breakdown,
                "warnings": [],
            }
        )
    results.sort(key=lambda item: float(item.get("rank_score", item.get("heat", 0))), reverse=True)
    results = results[: int(limit or CONFIG["max_targets_per_run"])]
    output_path = config_path("output_file", "outputs/hotspots.json")
    try_write_text(output_path, json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "topic",
        "topic": topic,
        "search_terms": topic_search_terms(topic),
        "target_search": compact_target_search_context(target_context),
        "timeframe_months": timeframe_months,
        "max_papers": max_papers or "unlimited",
        "topic_summary": topic_summary,
        "corpus_file": str(corpus_path),
        "papers": [serialize_ranked_paper(topic, paper, paper_rank_map) for paper in papers],
        "items": results,
        "validation": validate_extracted_hotspots(results),
    }
    log_event("topic analysis completed", {"topic": topic, "papers": len(papers), "hotspots": len(results)})
    return output


def fallback_papers(target: Target) -> list[Paper]:
    name = target.name or f"TIC {target.id}"
    current_year = date.today().year
    templates = [
        ("Nearby stellar sample refinement for optical interferometry missions", "目标样本、宜居带角距离与成像可行性分析。"),
        ("Stellar activity constraints for direct imaging candidate selection", "恒星活动性、观测稳定性与后续筛选优先级。"),
    ]
    return [
        Paper(
            title=f"{title}: {name}",
            authors=["Literature Assistant"],
            abstract=f"{name} 相关的{abstract}",
            published_at=str(current_year - index),
            year=current_year - index,
            url="",
            source="offline",
            target_id=target.id,
            relevance=0.45 - index * 0.05,
        )
        for index, (title, abstract) in enumerate(templates)
    ]


def collect_papers(target: Target) -> list[Paper]:
    papers = []
    papers.extend(ads_search(target))
    papers.extend(arxiv_search(target))
    if not papers:
        papers = fallback_papers(target)
    return dedupe_papers(papers)[: int(CONFIG["max_papers_per_target"])]


def fallback_papers(target: Target, timeframe_months: int | None = None) -> list[Paper]:
    name = target.name or f"TIC {target.id}"
    today = date.today()
    templates = [
        ("Nearby stellar sample refinement for optical interferometry missions", "target sample, habitable-zone angular separation, and imaging feasibility analysis."),
        ("Stellar activity constraints for direct imaging candidate selection", "stellar activity, observing stability, and downstream screening priority."),
    ]
    return [
        Paper(
            title=f"{title}: {name}",
            authors=["Literature Assistant"],
            abstract=f"{name}: {abstract}",
            published_at=(today - timedelta(days=index * 30)).isoformat(),
            year=(today - timedelta(days=index * 30)).year,
            url="",
            source="offline",
            target_id=target.id,
            relevance=0.45 - index * 0.05,
        )
        for index, (title, abstract) in enumerate(templates)
    ]


def collect_papers(target: Target, timeframe_months: int | None = None) -> list[Paper]:
    papers = []
    papers.extend(ads_search(target, timeframe_months))
    papers.extend(arxiv_search(target, timeframe_months))
    if not papers:
        papers = fallback_papers(target, timeframe_months)
    return dedupe_papers(papers)[: int(CONFIG["max_papers_per_target"])]


def deepseek_analyze(target: Target, papers: list[Paper]) -> Analysis:
    if not CONFIG.get("deepseek_enabled", True) or not os.getenv("DEEPSEEK_API_KEY"):
        return heuristic_analyze(target, papers)
    key_material = target.id + json.dumps([asdict(item) for item in papers], ensure_ascii=False)
    cached = read_cache("deepseek", key_material, max_age_hours=24 * 30)
    if cached:
        return Analysis(**cached)
    prompt = {
        "target": asdict(target),
        "papers": [asdict(item) for item in papers],
        "task": "请用中文分析这些天文文献与观测目标的关系，输出 JSON：summary, topics, conclusion, significance, related_targets, relevance_score。",
    }
    request_body = {
        "model": CONFIG.get("deepseek_model", "deepseek-chat"),
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你是天文任务规划文献分析助手，只输出可解析 JSON。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    request = urllib.request.Request(
        os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions"),
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        content = content.strip().removeprefix("```json").removesuffix("```").strip()
        parsed = json.loads(content)
        analysis = Analysis(
            summary=str(parsed.get("summary", "")),
            topics=list(parsed.get("topics", []))[:6],
            conclusion=str(parsed.get("conclusion", "")),
            significance=str(parsed.get("significance", "")),
            related_targets=[str(item) for item in parsed.get("related_targets", [])],
            relevance_score=float(parsed.get("relevance_score", 0.5)),
            provider="deepseek",
        )
        write_cache("deepseek", key_material, asdict(analysis))
        return analysis
    except Exception as exc:
        log_event("DeepSeek 分析失败，已回退到本地分析", {"target": target.id, "error": str(exc)})
        return heuristic_analyze(target, papers)


def heuristic_analyze(target: Target, papers: list[Paper]) -> Analysis:
    name = target.name or f"TIC {target.id}"
    text = " ".join(f"{paper.title} {paper.abstract}" for paper in papers).lower()
    topic_map = {
        "exoplanet": "系外行星",
        "habitable": "宜居带",
        "interferometry": "光学干涉",
        "direct imaging": "直接成像",
        "stellar activity": "恒星活动性",
        "biosignature": "生命指征",
    }
    topics = [label for keyword, label in topic_map.items() if keyword in text] or ["候选目标筛选", "邻近恒星样本"]
    relevance = max([paper.relevance for paper in papers] or [0.35])
    summary = f"{name} 相关文献主要涉及{'、'.join(topics[:3])}。系统根据论文数量、近期程度和目标别名命中情况给出可追溯热度。"
    return Analysis(
        summary=summary,
        topics=topics[:6],
        conclusion="该目标可作为观测序列内部的候选热点目标参与排序。",
        significance="分析结果用于辅助 AstroLens 进行后续目标筛选和任务规划。",
        related_targets=[target.id],
        relevance_score=round(float(relevance), 3),
        provider="heuristic",
    )


def score_target(target: Target, papers: list[Paper], analysis: Analysis, timeframe_months: int | None = None) -> dict[str, Any]:
    cutoff = cutoff_for_months(timeframe_months or int(CONFIG["recent_years"]) * 12)
    count_score = min(35.0, len(papers) * 7.0)
    recent_count = sum(1 for paper in papers if paper_date(paper) and cutoff and paper_date(paper) >= cutoff)
    recent_score = min(25.0, recent_count * 8.5)
    relevance_score = min(25.0, analysis.relevance_score * 25.0)
    representative_score = min(15.0, sum(1 for paper in papers if paper.relevance >= 0.35) * 5.0)
    heat = round(count_score + recent_score + relevance_score + representative_score, 1)
    return {
        "heat": min(100.0, heat),
        "score_breakdown": {
            "paper_count": round(count_score, 1),
            "recent_attention": round(recent_score, 1),
            "llm_relevance": round(relevance_score, 1),
            "representative": round(representative_score, 1),
        },
    }


def validate_hotspots(hotspots: list[dict[str, Any]], targets: list[Target]) -> dict[str, Any]:
    target_ids = {target.id for target in targets}
    missing_fields = []
    unmatched = []
    duplicate_ids = []
    seen = set()
    for item in hotspots:
        if "id" not in item or "heat" not in item:
            missing_fields.append(item)
        item_id = str(item.get("id", ""))
        if item_id in seen:
            duplicate_ids.append(item_id)
        seen.add(item_id)
        if item_id and item_id not in target_ids:
            unmatched.append(item_id)
    return {
        "valid": not missing_fields and not duplicate_ids,
        "target_count": len(targets),
        "hotspot_count": len(hotspots),
        "matched_count": len(hotspots) - len(unmatched),
        "unmatched_ids": unmatched,
        "duplicate_ids": duplicate_ids,
        "missing_fields_count": len(missing_fields),
    }


def build_hotspots(limit: int | None = None, use_seed: bool = False, timeframe_months: int | None = None) -> dict[str, Any]:
    ensure_dirs()
    timeframe_months = int(timeframe_months or CONFIG.get("default_timeframe_months", 12))
    targets = load_targets()
    target_map = {target.id: target for target in targets}
    selected_targets = targets[: limit or int(CONFIG["max_targets_per_run"])]
    results: list[dict[str, Any]] = []
    if use_seed:
        for seed in load_seed_hotspots():
            item_id = str(seed.get("id", ""))
            target = target_map.get(item_id)
            results.append(
                {
                    "id": item_id,
                    "name": target.name if target else None,
                    "heat": float(seed.get("heat", 0)),
                    "matched": target is not None,
                    "papers": [],
                    "summary": seed.get("comment") or ("样例热点记录已匹配观测序列。" if target else "样例热点记录未匹配当前观测序列。"),
                    "updated_at": date.today().isoformat(),
                    "timeframe_months": timeframe_months,
                    "score_breakdown": {},
                    "warnings": [] if target else ["hotspot_id_not_found_in_targets"],
                }
            )
    else:
        for target in selected_targets:
            papers = collect_papers(target, timeframe_months)
            analysis = deepseek_analyze(target, papers)
            score = score_target(target, papers, analysis, timeframe_months)
            results.append(
                {
                    "id": target.id,
                    "name": target.name,
                    "heat": score["heat"],
                    "matched": True,
                    "papers": [
                        serialize_paper_for_topic(target.name or target.id, paper)
                        for paper in papers[:3]
                    ],
                    "summary": analysis.summary,
                    "topics": analysis.topics,
                    "significance": analysis.significance,
                    "analysis_provider": analysis.provider,
                    "updated_at": date.today().isoformat(),
                    "timeframe_months": timeframe_months,
                    "score_breakdown": score["score_breakdown"],
                    "warnings": [],
                }
            )
    results.sort(key=lambda item: float(item.get("heat", 0)), reverse=True)
    output = {"generated_at": datetime.now().isoformat(timespec="seconds"), "timeframe_months": timeframe_months, "items": results, "validation": validate_hotspots(results, targets)}
    output_path = config_path("output_file", "outputs/hotspots.json")
    try_write_text(output_path, json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("热点分析完成", {"count": len(results), "output": str(output_path)})
    return output


def serialize_targets(limit: int | None = None) -> list[dict[str, Any]]:
    seed_map = {str(item.get("id")): item for item in load_seed_hotspots()}
    targets = load_targets()
    if limit:
        targets = targets[:limit]
    return [
        {
            "id": target.id,
            "name": target.name,
            "aliases": target.aliases,
            "ra_deg": target.ra_deg,
            "dec_deg": target.dec_deg,
            "vmag": target.vmag,
            "teff_k": target.teff_k,
            "seed_heat": seed_map.get(target.id, {}).get("heat"),
        }
        for target in targets
    ]


def json_response(handler: SimpleHTTPRequestHandler, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError) as exc:
        if getattr(exc, "winerror", None) in {10053, 10054} or isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            log_event("client disconnected before response completed", {"path": getattr(handler, "path", ""), "error": str(exc)})
            return
        raise


def require_api_password(handler: SimpleHTTPRequestHandler, query: dict[str, list[str]] | None = None, payload: dict[str, Any] | None = None) -> bool:
    expected_password = os.getenv("API_PASSWORD", "").strip()
    if not expected_password:
        return True

    provided = None
    if query is not None:
        provided = str(query.get("password", [""])[0] or "").strip()
    if not provided and payload is not None:
        provided = str(payload.get("password") or payload.get("api_password") or "").strip()
    if not provided:
        auth_header = handler.headers.get("Authorization", "") or ""
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                _, basic_password = decoded.split(":", 1)
                provided = basic_password.strip()
            except Exception:
                provided = ""
        elif auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        elif auth_header.startswith("Password "):
            provided = auth_header[9:].strip()
        else:
            provided = auth_header.strip()

    if not provided:
        json_response(
            handler,
            {"ok": False, "error": "Unauthorized"},
            HTTPStatus.UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="Literature Assistant"'},
        )
        return False

    if provided != expected_password:
        json_response(handler, {"ok": False, "error": "Forbidden"}, HTTPStatus.FORBIDDEN)
        return False
    return True


class LiteratureAssistantHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if not require_api_password(self, query=query):
                return
            if path == "/api/status":
                payload = {
                    "ok": True,
                    "config": {key: value for key, value in CONFIG.items() if "key" not in key.lower()},
                    "targets": len(load_targets()),
                    "ads_enabled": bool(os.getenv("ADS_API_KEY")),
                    "deepseek_enabled": bool(os.getenv("DEEPSEEK_API_KEY")),
                    "mineru_enabled": bool(mineru_api_token()),
                    "default_timeframe_months": int(CONFIG.get("default_timeframe_months", 12)),
                    "storage_base_dir": clean_text(str(CONFIG.get("storage_base_dir") or "")),
                    "resolved_storage_base_dir": str(configured_storage_base() or (ROOT / "outputs")),
                }
                json_response(self, payload)
            elif path == "/api/targets":
                limit = int(query.get("limit", [0])[0] or 0)
                json_response(self, serialize_targets(limit or None))
            elif path == "/api/deepseek/config":
                json_response(
                    self,
                    {
                        "enabled": bool(os.getenv("DEEPSEEK_API_KEY")),
                        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/chat/completions"),
                        "model": CONFIG.get("deepseek_model", "deepseek-chat"),
                        "key_preview": ("已配置" if os.getenv("DEEPSEEK_API_KEY") else "未配置"),
                    },
                )
            elif path == "/api/mineru/config":
                json_response(self, mineru_config_payload())
            elif path == "/api/seed-hotspots":
                targets = load_targets()
                seeds = load_seed_hotspots()
                json_response(self, {"items": seeds, "validation": validate_hotspots(seeds, targets)})
            elif path == "/api/paper-records":
                records = load_paper_records()
                paper_id = clean_text(str(query.get("paper_id", [""])[0] or query.get("id", [""])[0] or ""))
                if paper_id:
                    record = records.get(paper_id)
                    json_response(self, asdict(record) if record else {"ok": False, "error": "paper record not found"}, HTTPStatus.OK if record else HTTPStatus.NOT_FOUND)
                else:
                    json_response(self, {"items": [asdict(record) for record in records.values()]})
            elif path == "/api/paper/pdf":
                paper_id = clean_text(str(query.get("paper_id", [""])[0] or query.get("id", [""])[0] or ""))
                record = load_paper_records().get(paper_id)
                if not record or not record.pdf_path:
                    json_response(self, {"ok": False, "error": "PDF not found"}, HTTPStatus.NOT_FOUND)
                    return
                pdf_path = resolve_record_pdf_path(record)
                if not pdf_path:
                    json_response(self, {"ok": False, "error": "PDF file missing"}, HTTPStatus.NOT_FOUND)
                    return
                body = pdf_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'inline; filename="{pdf_path.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/paper/parse-task":
                task_id = clean_text(str(query.get("task_id", [""])[0] or ""))
                if task_id:
                    task = load_parse_task(task_id)
                    if task:
                        task = refresh_parse_task(task)
                    json_response(self, asdict(task) if task else {"ok": False, "error": "parse task not found"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
                else:
                    paper_id = clean_text(str(query.get("paper_id", [""])[0] or ""))
                    if not paper_id:
                        json_response(self, {"ok": False, "error": "missing task_id or paper_id"}, HTTPStatus.BAD_REQUEST)
                        return
                    tasks = []
                    for task_file in parse_tasks_dir().glob("*.json"):
                        try:
                            task_data = json.loads(task_file.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            continue
                        if task_data.get("paper_id") == paper_id:
                            task = load_parse_task(str(task_data.get("task_id") or ""))
                            if task:
                                task_data = asdict(refresh_parse_task(task))
                            tasks.append(task_data)
                    tasks.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
                    json_response(self, {"items": tasks})
            elif path == "/api/paper/markdown":
                paper_id = clean_text(str(query.get("paper_id", [""])[0] or query.get("id", [""])[0] or ""))
                record = load_paper_records().get(paper_id)
                if not record:
                    json_response(self, {"ok": False, "error": "paper record not found"}, HTTPStatus.NOT_FOUND)
                    return
                md_path = resolve_record_markdown_path(record)
                if not md_path:
                    json_response(self, {"ok": False, "error": "Markdown not found or parse not successful"}, HTTPStatus.NOT_FOUND)
                    return
                body = md_path.read_text(encoding="utf-8", errors="replace").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f'inline; filename="{md_path.name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/paper/science":
                paper_id = clean_text(str(query.get("paper_id", [""])[0] or query.get("id", [""])[0] or ""))
                if not paper_id:
                    json_response(self, {"ok": False, "error": "missing paper_id"}, HTTPStatus.BAD_REQUEST)
                    return
                science = load_science_records().get(paper_id)
                record = load_paper_records().get(paper_id)
                if (not science or (science.source != "markdown" and record and resolve_record_markdown_path(record))) and record:
                    science = science_record_for_record(record)
                if not science:
                    json_response(self, {"ok": False, "error": "science record not found; parse Markdown or pass paper payload first"}, HTTPStatus.NOT_FOUND)
                    return
                json_response(self, {"ok": True, "record": asdict(science)})
            elif path == "/api/hotspots":
                output_path = config_path("output_file", "outputs/hotspots.json")
                if output_path.exists():
                    items = json.loads(output_path.read_text(encoding="utf-8"))
                    json_response(self, {"items": items, "validation": validate_hotspots(items, load_targets())})
                else:
                    json_response(self, build_hotspots(use_seed=True))
            elif path == "/api/export":
                output_path = config_path("output_file", "outputs/hotspots.json")
                if not output_path.exists():
                    build_hotspots(use_seed=True)
                body = output_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=hotspots.json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()
        except Exception as exc:
            log_event("请求处理失败", {"path": path, "error": str(exc)})
            json_response(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if parsed.path == "/api/paper/upload-pdf":
                raw_body = self.rfile.read(length) if length else b""
                fields, files = parse_multipart_form(self.headers.get("Content-Type", ""), raw_body)
                if not require_api_password(self, payload=fields):
                    return
                paper_payload: dict[str, Any] = {}
                if fields.get("paper"):
                    parsed_paper = json.loads(fields["paper"])
                    if isinstance(parsed_paper, dict):
                        paper_payload = parsed_paper
                if fields.get("paper_id"):
                    paper_payload["paper_id"] = fields["paper_id"]
                paper = paper_from_payload(paper_payload)
                upload = files.get("pdf") or files.get("file")
                if not upload:
                    json_response(self, {"ok": False, "error": "missing pdf file"}, HTTPStatus.BAD_REQUEST)
                    return
                if not paper.title:
                    paper.title = Path(str(upload.get("filename") or "uploaded.pdf")).stem
                record = save_uploaded_paper_pdf(paper, paper_payload, bytes(upload.get("content") or b""), str(upload.get("filename") or "uploaded.pdf"))
                json_response(self, {"ok": True, "record": asdict(record)})
                return
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(body or "{}")
            if parsed.path == "/api/run":
                if not require_api_password(self, payload=payload):
                    return
                limit = int(payload.get("limit") or CONFIG["max_targets_per_run"])
                use_seed = bool(payload.get("use_seed", False))
                timeframe_months = int(payload.get("timeframe_months") or CONFIG.get("default_timeframe_months", 12))
                max_papers = int(payload.get("max_papers")) if payload.get("max_papers") is not None else int(CONFIG.get("max_topic_papers", 200))
                topic = clean_text(str(payload.get("topic") or ""))
                if topic:
                    json_response(self, build_topic_hotspots(topic=topic, limit=limit, timeframe_months=timeframe_months, max_papers=max_papers))
                else:
                    json_response(self, build_hotspots(limit=limit, use_seed=use_seed, timeframe_months=timeframe_months))
            elif parsed.path == "/api/paper/analyze":
                if not require_api_password(self, payload=payload):
                    return
                topic = clean_text(str(payload.get("topic") or ""))
                paper_payload = payload.get("paper") if isinstance(payload.get("paper"), dict) else payload
                paper = paper_from_payload(paper_payload)
                if not paper.title:
                    json_response(self, {"ok": False, "error": "missing paper title"}, HTTPStatus.BAD_REQUEST)
                else:
                    json_response(self, deepseek_single_paper_analysis(topic, paper))
            elif parsed.path == "/api/paper/fetch-pdf":
                if not require_api_password(self, payload=payload):
                    return
                paper_payload = payload.get("paper") if isinstance(payload.get("paper"), dict) else payload
                paper = paper_from_payload(paper_payload)
                if not paper.title:
                    json_response(self, {"ok": False, "error": "missing paper title"}, HTTPStatus.BAD_REQUEST)
                else:
                    record = fetch_and_save_paper_pdf(paper, paper_payload)
                    status = HTTPStatus.OK if record.fetch_status == "success" else HTTPStatus.BAD_GATEWAY
                    if record.fetch_status == "no_open_fulltext":
                        status = HTTPStatus.NOT_FOUND
                    json_response(self, {"ok": record.fetch_status == "success", "record": asdict(record)}, status)
            elif parsed.path == "/api/paper/parse":
                if not require_api_password(self, payload=payload):
                    return
                records = load_paper_records()
                paper_id = clean_text(str(payload.get("paper_id") or payload.get("id") or ""))
                paper_payload = payload.get("paper") if isinstance(payload.get("paper"), dict) else payload
                if not paper_id and isinstance(paper_payload, dict):
                    paper = paper_from_payload(paper_payload)
                    paper_id = paper_id_for(paper, paper_payload)
                record = records.get(paper_id)
                if not record:
                    json_response(self, {"ok": False, "error": "paper PDF record not found; fetch PDF first"}, HTTPStatus.NOT_FOUND)
                    return
                task = start_parse_task_for_record(record)
                status = HTTPStatus.ACCEPTED if task.status in {"queued", "running"} else HTTPStatus.OK
                if task.status == "failed":
                    status = HTTPStatus.BAD_REQUEST
                json_response(self, {"ok": task.status not in {"failed"}, "task": asdict(task), "record": task.record}, status)
            elif parsed.path == "/api/paper/extract-science":
                if not require_api_password(self, payload=payload):
                    return
                force = bool(payload.get("force", False))
                paper_id = clean_text(str(payload.get("paper_id") or payload.get("id") or ""))
                paper_payload = payload.get("paper") if isinstance(payload.get("paper"), dict) else payload
                record = load_paper_records().get(paper_id) if paper_id else None
                if record:
                    science = science_record_for_record(record, force=force)
                elif isinstance(paper_payload, dict):
                    paper = paper_from_payload(paper_payload)
                    if not paper.title:
                        json_response(self, {"ok": False, "error": "missing paper title"}, HTTPStatus.BAD_REQUEST)
                        return
                    science = science_record_for_paper(paper, force=force)
                else:
                    json_response(self, {"ok": False, "error": "missing paper_id or paper payload"}, HTTPStatus.BAD_REQUEST)
                    return
                json_response(self, {"ok": True, "record": asdict(science)})
            elif parsed.path == "/api/deepseek/config":
                if not require_api_password(self, payload=payload):
                    return
                api_key = str(payload.get("api_key") or "").strip()
                base_url = str(payload.get("base_url") or "https://api.deepseek.com/chat/completions").strip()
                model = str(payload.get("model") or "deepseek-chat").strip()
                values = {
                    "DEEPSEEK_BASE_URL": base_url,
                    "DEEPSEEK_MODEL": model,
                }
                if api_key:
                    values["DEEPSEEK_API_KEY"] = api_key
                write_env_values(values)
                CONFIG["deepseek_model"] = model
                json_response(
                    self,
                    {
                        "ok": True,
                        "enabled": bool(os.getenv("DEEPSEEK_API_KEY")),
                        "base_url": os.getenv("DEEPSEEK_BASE_URL", base_url),
                        "model": model,
                    },
                )
            elif parsed.path == "/api/mineru/config":
                if not require_api_password(self, payload=payload):
                    return
                adapter = dict(CONFIG.get("mineru_adapter") or {})
                mode = clean_text(str(payload.get("mode") or adapter.get("mode") or "command")).lower()
                if mode not in {"command", "official_api", "api", "mineru_api", "service"}:
                    mode = "command"
                api_base = clean_text(str(payload.get("api_base") or adapter.get("api_base") or "https://mineru.net")).rstrip("/")
                model_version = clean_text(str(payload.get("api_model_version") or adapter.get("api_model_version") or "vlm"))
                api_language = clean_text(str(payload.get("api_language") or adapter.get("api_language") or "en"))
                command = clean_text(str(payload.get("command") or adapter.get("command") or "mineru"))
                service_url = clean_text(str(payload.get("service_url") or adapter.get("service_url") or ""))
                poll_interval = max(2, int(payload.get("api_poll_interval_seconds") or adapter.get("api_poll_interval_seconds") or 5))
                storage_base_dir = clean_text(str(payload.get("storage_base_dir") or ""))
                config_update = {
                    "storage_base_dir": storage_base_dir,
                    "mineru_adapter": {
                        "mode": mode,
                        "command": command,
                        "service_url": service_url,
                        "api_base": api_base,
                        "api_model_version": model_version,
                        "api_language": api_language,
                        "api_enable_formula": bool(payload.get("api_enable_formula", adapter.get("api_enable_formula", True))),
                        "api_enable_table": bool(payload.get("api_enable_table", adapter.get("api_enable_table", True))),
                        "api_poll_interval_seconds": poll_interval,
                    },
                }
                write_config_values(config_update)
                api_token = str(payload.get("api_token") or "").strip()
                if api_token:
                    write_env_values({"MINERU_API_TOKEN": api_token})
                ensure_dirs()
                json_response(self, {"ok": True, **mineru_config_payload()})
            elif parsed.path == "/api/validate":
                if not require_api_password(self, payload=payload):
                    return
                hotspots = payload.get("items", [])
                json_response(self, validate_hotspots(hotspots, load_targets()))
            else:
                json_response(self, {"ok": False, "error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            log_event("POST 请求处理失败", {"path": parsed.path, "error": str(exc)})
            json_response(self, {"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description="文献助手本地服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5179)
    parser.add_argument("--run-parse-task", help="运行一个文件型 PDF 转 Markdown 解析任务后退出")
    args = parser.parse_args()
    ensure_dirs()
    if args.run_parse_task:
        raise SystemExit(run_parse_task(args.run_parse_task))
    server = ThreadingHTTPServer((args.host, args.port), LiteratureAssistantHandler)
    print(f"文献助手已启动: http://{args.host}:{args.port}/index.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
