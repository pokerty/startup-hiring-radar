#!/usr/bin/env python3
"""Discover promising, Singapore-connected startup engineering teams with Exa."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv
from exa_py import Exa
from pydantic import BaseModel, Field, ValidationError


LOG = logging.getLogger("startup-hiring-radar")
CACHE_TTL = timedelta(hours=24)
EXCLUDED_DOMAINS = ["linkedin.com", "indeed.com", "glassdoor.com"]
MEGA_CAP_NAMES = {
    "airbnb",
    "apple",
    "amazon",
    "bytedance",
    "dbs",
    "dbs bank",
    "goldman",
    "goldman sachs",
    "google",
    "grab",
    "jpmorgan",
    "jpmorgan chase",
    "lazada",
    "mastercard",
    "meta",
    "microsoft",
    "nvidia",
    "sea",
    "sea group",
    "sea limited",
    "shopee",
    "stripe",
    "tiktok",
    "uber",
    "visa",
}

DISCOVERY_QUERIES = (
    "Recent Series A, B, or C funding rounds for AI, developer tools, or fintech "
    "startups, especially companies building or hiring engineering teams in Singapore.",
    "Recent announcements of Singapore engineering hub expansions or founder and CTO "
    "hiring at AI, developer tools, or fintech startups.",
    "Recent founder, CTO, or engineering leadership hiring announcements at fast-growing "
    "AI, developer tools, or fintech startups with Singapore presence.",
)

DISCOVERY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "companies": {"type": "array", "items": {"type": "string"}},
        "signal_summary": {"type": "string"},
    },
    "required": ["companies", "signal_summary"],
    "additionalProperties": False,
}

COMPANY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string"},
        "domain": {"type": "string"},
        "company_intro": {
            "type": "string",
            "description": (
                "A brief factual introduction: if verified, name the founders, "
                "founding country and year, what the company builds, who it serves, "
                "and notable technology such as AI agents. Do not guess missing facts."
            ),
        },
        "funding_stage": {
            "type": "string",
            "description": "Most recent round stage only (for example Series A); do not include amount.",
        },
        "latest_funding_date": {
            "type": "string",
            "description": (
                "Date of the most recent funding round found within the requested "
                "news window, preferably Month YYYY; otherwise say not verified."
            ),
        },
        "funding_amount": {
            "type": "string",
            "description": (
                "Amount raised in that same round with currency, or say not verified; "
                "do not infer an amount from total lifetime funding."
            ),
        },
        "funding_source_url": {
            "type": "string",
            "description": "Direct URL of the source reporting that latest funding round, or empty.",
        },
        "sg_presence": {"type": "string"},
        "hiring_roles": {"type": "array", "items": {"type": "string"}},
        "careers_url": {"type": "string"},
    },
    "required": [
        "company_name",
        "domain",
        "company_intro",
        "funding_stage",
        "latest_funding_date",
        "funding_amount",
        "funding_source_url",
        "sg_presence",
        "hiring_roles",
        "careers_url",
    ],
    "additionalProperties": False,
}

def _schema_for(fields: tuple[str, ...]) -> dict[str, Any]:
    properties = COMPANY_OUTPUT_SCHEMA["properties"]
    return {
        "type": "object",
        "properties": {field: properties[field] for field in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


FUNDING_OUTPUT_SCHEMA = _schema_for(
    ("funding_stage", "latest_funding_date", "funding_amount", "funding_source_url")
)
CAREERS_OUTPUT_SCHEMA = _schema_for(
    ("company_name", "domain", "company_intro", "sg_presence", "hiring_roles", "careers_url")
)


class CompanyEnrichment(BaseModel):
    company_name: str
    domain: str = ""
    company_intro: str = ""
    funding_stage: str = ""
    latest_funding_date: str = ""
    funding_amount: str = ""
    funding_source_url: str = ""
    sg_presence: str = ""
    hiring_roles: list[str] = Field(default_factory=list)
    careers_url: str = ""
    why_now_hook: str = ""


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find fast-moving, well-funded startups hiring engineers."
    )
    parser.add_argument(
        "--limit", type=positive_int, default=5, help="maximum companies to enrich (default: 5)"
    )
    parser.add_argument(
        "--days", type=positive_int, default=60, help="signal lookback window (default: 60)"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="ignore cached responses and fetch fresh results"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("."),
        help="directory for timestamped targets files (default: current directory)",
    )
    return parser.parse_args(argv)


def _read_attr(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _jsonable(method())
            except (TypeError, ValueError):
                continue
    return str(value)


def _response_payload(response: Any) -> dict[str, Any]:
    output = _read_attr(response, "output")
    content = _read_attr(output, "content")
    results = _read_attr(response, "results", []) or []
    normalized_results: list[dict[str, Any]] = []
    for result in results:
        normalized_results.append(
            {
                "title": _jsonable(_read_attr(result, "title", "")),
                "url": _jsonable(_read_attr(result, "url", "")),
                "published_date": _jsonable(_read_attr(result, "published_date", "")),
                "highlights": _jsonable(_read_attr(result, "highlights", [])),
                "text": _jsonable(_read_attr(result, "text", "")),
            }
        )
    return {"output_content": _jsonable(content), "results": normalized_results}


class JsonCache:
    def __init__(self, path: Path, refresh: bool = False) -> None:
        self.path = path
        self.entries: dict[str, Any] = {}
        self.enabled = not refresh
        if self.enabled and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.entries = loaded
                    for key in list(self.entries):
                        self._is_fresh(key)
                else:
                    LOG.warning("Cache root is not an object; starting with an empty cache.")
            except (OSError, json.JSONDecodeError) as exc:
                LOG.warning("Could not read cache (%s); starting with an empty cache.", exc)

    @staticmethod
    def _key(stage: str, query: str, kwargs: dict[str, Any]) -> str:
        cache_input = json.dumps(
            {"stage": stage, "query": query, "kwargs": kwargs},
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(cache_input.encode("utf-8")).hexdigest()
        return f"{stage}:{digest}"

    def _is_fresh(self, key: str) -> bool:
        entry = self.entries.get(key)
        if not isinstance(entry, dict) or "payload" not in entry:
            self.entries.pop(key, None)
            return False
        try:
            cached_at = datetime.fromisoformat(str(entry["cached_at"]))
        except (KeyError, TypeError, ValueError):
            self.entries.pop(key, None)
            return False
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - cached_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > CACHE_TTL:
            self.entries.pop(key, None)
            return False
        return True

    def get(self, stage: str, query: str, kwargs: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        key = self._key(stage, query, kwargs)
        if not self._is_fresh(key):
            return None
        return self.entries[key]["payload"]

    def contains(self, stage: str, query: str, kwargs: dict[str, Any]) -> bool:
        return self.enabled and self._is_fresh(self._key(stage, query, kwargs))

    def put(self, stage: str, query: str, kwargs: dict[str, Any], value: Any) -> None:
        self.entries[self._key(stage, query, kwargs)] = {
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "payload": value,
        }
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as handle:
                json.dump(self.entries, handle, ensure_ascii=False, indent=2)
                temporary_path = Path(handle.name)
            os.replace(temporary_path, self.path)
        except OSError as exc:
            LOG.warning("Could not save cache at %s: %s", self.path, exc)
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


class ExaSearch:
    def __init__(self, client: Exa, cache: JsonCache) -> None:
        self.client = client
        self.cache = cache
        self.api_calls = 0

    def search(self, stage: str, query: str, **kwargs: Any) -> dict[str, Any]:
        if self.cache.contains(stage, query, kwargs):
            LOG.debug("Cache hit: %s", stage)
            return self.cache.get(stage, query, kwargs)
        self.api_calls += 1
        response = self.client.search(query, **kwargs)
        payload = _response_payload(response)
        self.cache.put(stage, query, kwargs, payload)
        return payload


def _structured_content(payload: dict[str, Any]) -> Any:
    content = payload.get("output_content")
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return content


def _company_names(payload: dict[str, Any]) -> list[str]:
    structured = _structured_content(payload)
    names = structured.get("companies", []) if isinstance(structured, dict) else []
    if not isinstance(names, list):
        return []
    return [name.strip() for name in names if isinstance(name, str) and name.strip()]


def _normalize_company_name(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", "", name.casefold()).split())


def _is_mega_cap(name: str) -> bool:
    normalized = _normalize_company_name(name)
    return normalized in MEGA_CAP_NAMES or any(
        normalized.startswith(f"{legacy} ") for legacy in MEGA_CAP_NAMES
    )


def discover_candidates(
    search: ExaSearch, days: int, queries: tuple[str, ...] = DISCOVERY_QUERIES
) -> list[dict[str, Any]]:
    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    discovered: dict[str, dict[str, Any]] = {}
    for index, query in enumerate(queries, start=1):
        stage = f"discovery:{days}d:{start_date}"
        try:
            payload = search.search(
                stage,
                query,
                type="auto",
                category="news",
                num_results=8,
                exclude_domains=EXCLUDED_DOMAINS,
                start_published_date=start_date,
                contents={"highlights": {"max_characters": 600}, "max_age_hours": 24},
                output_schema=DISCOVERY_OUTPUT_SCHEMA,
            )
            for name in _company_names(payload):
                normalized = _normalize_company_name(name)
                if normalized and not _is_mega_cap(name):
                    candidate = discovered.setdefault(
                        normalized,
                        {"name": name, "signals": []},
                    )
                    candidate_urls = {
                        signal["url"] for signal in candidate["signals"] if signal["url"]
                    }
                    for result in payload.get("results", []):
                        if not isinstance(result, dict):
                            continue
                        highlight = result.get("highlights")
                        if isinstance(highlight, list):
                            highlight_text = " ".join(_string_value(item) for item in highlight)
                        else:
                            highlight_text = _string_value(highlight)
                        searchable_text = " ".join(
                            (
                                _string_value(result.get("title")),
                                _string_value(result.get("url")),
                                highlight_text,
                                _string_value(result.get("text")),
                            )
                        )
                        normalized_text = f" {_normalize_company_name(searchable_text)} "
                        if f" {normalized} " not in normalized_text:
                            continue
                        url = _string_value(result.get("url"))
                        if url and url in candidate_urls:
                            continue
                        candidate["signals"].append(
                            {
                                "title": _string_value(result.get("title")),
                                "url": url,
                                "published_date": _string_value(result.get("published_date")),
                                "highlights": highlight_text,
                            }
                        )
                        if url:
                            candidate_urls.add(url)
            LOG.info("Signal query %d/%d complete.", index, len(queries))
        except Exception as exc:
            LOG.error("Signal query %d/%d failed: %s", index, len(queries), exc)
    return list(discovered.values())


def _latest_funding_result(results: Any) -> dict[str, str]:
    if not isinstance(results, list):
        return {}
    matches: list[dict[str, str]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        title = _string_value(result.get("title"))
        highlights = result.get("highlights")
        if isinstance(highlights, list):
            highlight = " ".join(_string_value(item) for item in highlights)
        else:
            highlight = _string_value(highlights)
        if not re.search(
            r"\b(?:funding|fundraise|financing|raised|raises|series\s+[a-f]|seed round)\b",
            f"{title} {highlight}",
            flags=re.IGNORECASE,
        ):
            continue
        matches.append(
            {
                "title": title,
                "url": _string_value(result.get("url")),
                "published_date": _string_value(result.get("published_date")),
                "highlights": highlight,
            }
        )
    if not matches:
        return {}

    def date_order(signal: dict[str, str]) -> float:
        try:
            published = datetime.fromisoformat(signal["published_date"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            return published.timestamp()
        except ValueError:
            return 0

    return max(matches, key=date_order)


def _discovery_context(candidate: dict[str, Any]) -> str:
    parts: list[str] = []
    for signal in candidate.get("signals", [])[:3]:
        parts.append(
            " | ".join(
                value
                for value in (
                    _string_value(signal.get("published_date")),
                    _string_value(signal.get("title")),
                    _string_value(signal.get("highlights")),
                    _string_value(signal.get("url")),
                )
                if value
            )
        )
    return "\n".join(parts) or "No matching Stage 1 article details were extracted."


def enrich_company(search: ExaSearch, candidate: dict[str, Any]) -> CompanyEnrichment | None:
    name = candidate["name"]
    start_date = (datetime.now(timezone.utc) - timedelta(days=180)).date().isoformat()
    context = _discovery_context(candidate)
    funding_query = (
        f"{name} latest funding round funding Series raised expansion Singapore "
        f"{datetime.now(timezone.utc).year}. Verify the most recent announced round "
        "within the requested date window; return its stage, announcement month/year, "
        "the exact amount raised in that round with currency, and a direct source URL. "
        "Do not infer the amount from lifetime funding or repeat older rounds as "
        "current. Recent Stage 1 evidence follows:\n"
        f"{context}"
    )
    careers_query = (
        f"{name} careers jobs software engineer Singapore. Find the official domain, "
        "current careers page or ATS jobs board, Singapore presence, and current "
        "Backend, Product, Forward Deployed, Software, Full Stack, AI, Solutions, "
        "or Founding Engineer roles. Also provide a brief factual introduction: "
        "founders, founding country and year when verified, what it builds, who it "
        "serves, and notable technology such as AI agents. Do not guess missing facts."
    )
    funding_content: dict[str, Any] = {}
    funding_result: dict[str, str] = {}
    try:
        funding_payload = search.search(
            "recent-funding",
            funding_query,
            type="auto",
            category="news",
            num_results=5,
            start_published_date=start_date,
            contents={"highlights": {"max_characters": 600}, "max_age_hours": 24},
            output_schema=FUNDING_OUTPUT_SCHEMA,
        )
        structured_funding = _structured_content(funding_payload)
        if isinstance(structured_funding, dict):
            funding_content = structured_funding
        else:
            LOG.warning("No structured recent-funding output for %s.", name)
        funding_result = _latest_funding_result(funding_payload.get("results", []))
    except Exception as exc:
        LOG.error("Recent funding search failed for %s: %s", name, exc)

    try:
        careers_payload = search.search(
            "company-careers",
            careers_query,
            type="auto",
            num_results=4,
            contents={"highlights": {"max_characters": 600}, "max_age_hours": 24},
            output_schema=CAREERS_OUTPUT_SCHEMA,
        )
        careers_content = _structured_content(careers_payload)
        if not isinstance(careers_content, dict):
            raise ValueError("structured careers/company output was missing or invalid")
        recent_signal = next(iter(candidate.get("signals", [])), {})
        combined = {
            **careers_content,
            **funding_content,
            "company_name": careers_content.get("company_name") or name,
            "funding_stage": funding_content.get("funding_stage")
            or "Not verified in the last 180 days",
            "latest_funding_date": funding_content.get("latest_funding_date")
            or (
                f"Round date unverified; source published {funding_result['published_date']}"
                if funding_result.get("published_date")
                else "Not verified in the last 180 days"
            ),
            "funding_amount": funding_content.get("funding_amount")
            or "Not verified in the last 180 days",
            "funding_source_url": funding_content.get("funding_source_url")
            or funding_result.get("url", ""),
            "why_now_hook": funding_result.get("highlights")
            or _string_value(recent_signal.get("highlights"))
            or _string_value(recent_signal.get("title")),
        }
        company = CompanyEnrichment.model_validate(combined)
        if _is_mega_cap(company.company_name):
            LOG.info("Skipping excluded legacy company: %s", company.company_name)
            return None
        if not _is_target_company(company):
            LOG.info(
                "Keeping %s as a lower-confidence target "
                "(funding, SG, or role signal is incomplete).",
                name,
            )
        return company
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        LOG.error("Could not parse company enrichment for %s: %s", name, exc)
    except Exception as exc:
        LOG.error("Company enrichment failed for %s: %s", name, exc)
    return None


def lookup_leads(search: ExaSearch, company: CompanyEnrichment) -> list[dict[str, str]]:
    identity = company.domain or company.company_name
    query = (
        f"Find engineering decision-makers at {company.company_name} ({identity}), "
        "prioritizing the CTO, Head of Engineering, VP of Engineering, or Engineering "
        "Manager, preferably based in Singapore. Return their name, role, professional "
        "profile URL, and a recent technical article, talk, or engineering project. "
        "Prefer leaders or hiring managers over individual-contributor engineers; "
        "only include an IC if no relevant leader is available."
    )
    try:
        payload = search.search(
            "people-lookup",
            query,
            type="auto",
            category="people",
            num_results=2,
            contents={"highlights": {"max_characters": 600}},
        )
    except Exception as exc:
        LOG.error("Lead lookup failed for %s: %s", company.company_name, exc)
        return []

    try:
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise ValueError("people results were not a list")
        leads: list[dict[str, str]] = []
        for result in results[:2]:
            if not isinstance(result, dict):
                continue
            title = _clean_prose(result.get("title", ""))
            url = _string_value(result.get("url"))
            highlights = result.get("highlights")
            if isinstance(highlights, list):
                raw_highlight = "\n".join(
                    _string_value(item) for item in highlights if item
                )
            else:
                raw_highlight = _string_value(highlights)
            if not raw_highlight:
                raw_highlight = _string_value(result.get("text"))
            highlight = _clean_prose(raw_highlight)[:280].rstrip()
            if title or url or highlight:
                leads.append({"name": title, "url": url, "highlight": highlight})
        return leads
    except (AttributeError, TypeError, ValueError) as exc:
        LOG.error("Could not parse lead results for %s: %s", company.company_name, exc)
        return []


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _clean_prose(value: Any) -> str:
    text = _string_value(value)
    if not text:
        return ""
    lines = [
        line
        for line in text.splitlines()
        if not re.match(r"^\s{0,3}#{1,6}(?:\s|$)", line)
    ]
    text = "\n".join(lines)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[#*`_~\[\]<>]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _email_draft(company: CompanyEnrichment, leads: list[dict[str, str]]) -> str:
    def phrase(value: str, fallback: str) -> str:
        cleaned = _clean_prose(value)
        first_sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
        return first_sentence.strip().rstrip(".!?") or fallback

    hook = phrase(company.why_now_hook, "your team's recent growth")
    roles = phrase(", ".join(company.hiring_roles), "engineering")
    angle = _safe_email_angle("")
    amount = _verified_funding_fact(company.funding_amount)
    funding_stage = _verified_funding_fact(company.funding_stage)
    funding_date = _verified_funding_fact(company.latest_funding_date)
    if amount and funding_stage:
        funding_stage = re.sub(
            re.escape(amount), "", funding_stage, flags=re.IGNORECASE
        ).strip(" ,:()")
    round_parts = [fact for fact in (amount, funding_stage) if fact]
    if round_parts:
        round_details = " ".join(round_parts)
        if "round" not in round_details.casefold():
            round_details = f"{round_details} round"
        date_detail = f" announced in {funding_date}" if funding_date else ""
        opening = (
            f"I saw your {round_details}{date_detail}, and was impressed by "
            f"{company.company_name}'s momentum."
        )
    elif funding_date:
        opening = (
            f"I saw your funding announcement from {funding_date}, and was impressed "
            f"by {company.company_name}'s momentum."
        )
    else:
        opening = (
            f"I noticed {hook} and was impressed by {company.company_name}'s momentum."
        )
    lead = leads[0] if leads else {}
    lead_name = phrase(lead.get("name", ""), "")
    greeting = f"Hi {lead_name}, " if lead_name else ""
    highlight = phrase(lead.get("highlight", ""), "")
    if highlight:
        second_sentence = f"I enjoyed your recent work on {highlight}."
    else:
        second_sentence = f"Your work around {roles} in Singapore caught my eye."
    return (
        f"{greeting}{opening} "
        f"{second_sentence} "
        f"I'd love to hear more about {angle} and how the team is scaling. "
        "Would you be open to a brief conversation next week?"
    )


def _verified_funding_fact(value: str) -> str:
    fact = _clean_prose(value)
    fact = re.split(r"(?<=[.!?])\s+", fact, maxsplit=1)[0].strip().rstrip(".!?")
    if re.search(
        r"\b(?:not verified|unverified|undisclosed|not disclosed|unknown|"
        r"not found|not available|not reported)\b",
        fact,
        flags=re.IGNORECASE,
    ):
        return ""
    return fact


def _safe_email_angle(value: str) -> str:
    angle = _clean_prose(value)
    angle = re.split(r"(?<=[.!?])\s+", angle, maxsplit=1)[0].strip().rstrip(".!?")
    angle = re.sub(
        r"^(?:please\s+)?(?:highlight|emphasize|focus on|discuss|explore|consider)"
        r"(?:\s+your interest in)?\s+",
        "",
        angle,
        flags=re.IGNORECASE,
    )
    angle = re.sub(r"^(?:your interest in|interest in)\s+", "", angle, flags=re.IGNORECASE)
    words = angle.split()
    if (
        len(words) < 5
        or len(words) > 10
        or re.match(
            r"^(?:i|we|you|your|please|to|learn|tell|share|ask|help|build|scale|"
            r"create|improve|reduce|drive|join|transform|deliver|get)\b",
            angle,
            flags=re.IGNORECASE,
        )
    ):
        return "scalable engineering and product systems"
    return angle[0].lower() + angle[1:]


def _is_target_company(company: CompanyEnrichment) -> bool:
    funding = company.funding_stage.casefold()
    has_target_funding = bool(
        re.search(r"\bseries\s+[abc]\b", funding)
        or re.search(r"\b(?:seed|pre[- ]?seed)\b", funding)
        or re.search(r"\$\s?\d+(?:\.\d+)?\s?(?:m|million|b|billion)\b", funding)
    )
    presence = company.sg_presence.casefold()
    has_singapore_presence = bool(re.search(r"\b(?:singapore|sg|yes)\b", presence))
    roles = " ".join(company.hiring_roles).casefold()
    has_target_role = any(
        value in roles
        for value in (
            "backend",
            "product engineer",
            "forward deployed",
            "forward-deployed",
            "software engineer",
            "full stack",
            "full-stack",
            "ai engineer",
            "solutions",
            "founding",
            "platform",
            "infrastructure",
        )
    ) or roles.strip() in {"product", "engineering"}
    return has_target_funding and has_singapore_presence and has_target_role


def _markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _google_freshness_url(company_name: str) -> str:
    query = f'"{company_name}" latest funding round startup'
    return f"https://www.google.com/search?q={quote_plus(query)}"


def write_markdown(targets: list[dict[str, Any]], path: Path) -> None:
    lines = ["# Startup Hiring Radar", ""]
    if not targets:
        lines.extend(["No qualifying startup targets were discovered in this run.", ""])
    for target in targets:
        company: CompanyEnrichment = target["company"]
        lines.extend(
            [
                f"## {_markdown_escape(company.company_name)}",
                "",
                "### Company introduction",
                "",
                _markdown_escape(company.company_intro)
                or "Not enough verified company background found.",
                "",
                f"- **Domain:** {_markdown_escape(company.domain) or 'Not found'}",
                f"- **Funding stage:** {_markdown_escape(company.funding_stage) or 'Not verified'}",
                f"- **Latest funding date:** {_markdown_escape(company.latest_funding_date) or 'Not verified in the last 180 days'}",
                f"- **Funding amount:** {_markdown_escape(company.funding_amount) or 'Not verified in the last 180 days'}",
                (
                    f"- **Funding source:** [Open source]({company.funding_source_url})"
                    if company.funding_source_url
                    else "- **Funding source:** Not verified"
                ),
                f"- **Singapore presence:** {_markdown_escape(company.sg_presence) or 'Not verified'}",
                f"- **Target fit:** {target['fit_tier']}",
                f"- **Why now:** {_markdown_escape(company.why_now_hook) or 'No specific hook found'}",
                f"- **Hiring roles:** {_markdown_escape(', '.join(company.hiring_roles)) or 'Not confirmed'}",
                f"- **Careers:** {company.careers_url or 'Not found'}",
                (
                    f"- **Freshness check before outreach:** "
                    f"[Google latest funding search]({_google_freshness_url(company.company_name)}) "
                    "(confirm round and date before emailing)"
                ),
                "",
            ]
        )
        signals = target.get("signals", [])
        if signals:
            lines.extend(["### Recent discovery signals", ""])
            for signal in signals:
                date = _markdown_escape(signal.get("published_date", ""))
                title = _markdown_escape(signal.get("title", "")) or "Recent signal"
                source_url = signal.get("url", "")
                label = f"{date}: {title}" if date else title
                lines.append(f"- [{label}]({source_url})" if source_url else f"- {label}")
                highlight = _markdown_escape(_clean_prose(signal.get("highlights", "")))
                if highlight:
                    lines.append(f"  - {_markdown_escape(highlight)}")
            lines.append("")
        lines.extend(["### Target leads", ""])
        leads = target["leads"]
        if not leads:
            lines.extend(["No relevant lead profiles found.", ""])
        for lead in leads:
            name = _markdown_escape(lead["name"]) or "Technical decision-maker"
            url = lead["url"]
            lines.append(f"- [{name}]({url})" if url else f"- {name}")
            if lead["highlight"]:
                lines.append(f"  - Recent work: {_markdown_escape(lead['highlight'])}")
        lines.extend(
            [
                "",
                "### Cold email draft",
                "",
                _email_draft(company, leads),
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


CSV_FIELDS = [
    "company_name",
    "domain",
    "company_intro",
    "funding_stage",
    "latest_funding_date",
    "funding_amount",
    "funding_source_url",
    "sg_presence",
    "hiring_roles",
    "careers_url",
    "why_now_hook",
    "freshness_check_url",
    "target_fit",
    "cold_email_draft",
    "lead_1_name",
    "lead_1_url",
    "lead_1_highlight",
    "lead_2_name",
    "lead_2_url",
    "lead_2_highlight",
]


def write_csv(targets: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for target in targets:
            company: CompanyEnrichment = target["company"]
            row: dict[str, Any] = {
                "company_name": company.company_name,
                "domain": company.domain,
                "company_intro": company.company_intro,
                "funding_stage": company.funding_stage,
                "latest_funding_date": company.latest_funding_date,
                "funding_amount": company.funding_amount,
                "funding_source_url": company.funding_source_url,
                "sg_presence": company.sg_presence,
                "hiring_roles": ", ".join(company.hiring_roles),
                "careers_url": company.careers_url,
                "why_now_hook": company.why_now_hook,
                "freshness_check_url": _google_freshness_url(company.company_name),
                "target_fit": target["fit_tier"],
                "cold_email_draft": _email_draft(company, target["leads"]),
            }
            for index in range(2):
                lead = target["leads"][index] if index < len(target["leads"]) else {}
                row.update(
                    {
                        f"lead_{index + 1}_name": lead.get("name", ""),
                        f"lead_{index + 1}_url": lead.get("url", ""),
                        f"lead_{index + 1}_highlight": lead.get("highlight", ""),
                    }
                )
            writer.writerow(row)


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        LOG.error("EXA_API_KEY is missing. Add it to your environment or a local .env file.")
        return 2

    cache = JsonCache(Path(".exa_cache.json"), refresh=args.refresh)
    search = ExaSearch(Exa(api_key), cache)
    max_attempts = args.limit * 3
    estimated_calls = len(DISCOVERY_QUERIES) + (3 * max_attempts)
    LOG.info(
        "Starting radar: up to %d targets (%d enrichment attempts max), %d-day signal window; "
        "estimated maximum %d Exa calls.",
        args.limit,
        max_attempts,
        args.days,
        estimated_calls,
    )

    candidates = discover_candidates(search, args.days)
    LOG.info("Discovered %d unique eligible company candidates.", len(candidates))
    targets: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        if index > max_attempts or len(targets) >= args.limit:
            break
        LOG.info(
            "Enriching company %d/%d: %s",
            index,
            min(len(candidates), max_attempts),
            candidate["name"],
        )
        company = enrich_company(search, candidate)
        if company is None:
            continue
        leads = lookup_leads(search, company)
        tier = "Tier 1" if _is_target_company(company) else "Tier 2"
        targets.append(
            {
                "company": company,
                "leads": leads,
                "fit_tier": tier,
                "signals": candidate["signals"],
            }
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    markdown_path = args.out_dir / f"targets_today_{timestamp}.md"
    csv_path = args.out_dir / f"targets_{timestamp}.csv"
    write_markdown(targets, markdown_path)
    write_csv(targets, csv_path)
    LOG.info(
        "Wrote %d targets to %s and %s; Exa API calls this run: %d "
        "(estimated maximum: %d).",
        len(targets),
        markdown_path,
        csv_path,
        search.api_calls,
        estimated_calls,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
