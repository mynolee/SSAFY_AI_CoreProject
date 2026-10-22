"""Validated local fact metadata and conservative condition search.

This module never fetches a source or treats a source link as document text.
Unknown values stay unknown, including in filtered search results.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .errors import ServiceError


_ITEM_TEXT = (
    "item_id", "name", "provider", "product_group", "source_offer_name",
    "device_category", "monthly_fee_basis", "source_id", "source_url",
    "source_locator", "verified_on", "sale_status", "verification_status", "index_text",
)
_NULLABLE_TEXT = ("product_id", "source_group_product_id", "source_version_note")
_FACT_STATUS = "NAMED_SOURCE_ENTRY_AND_RECORDED_FACTS_VERIFIED"
_RELEASE_STATUS = "NOT_CLEARED_DATABASE_SCOPE_UNRESOLVED"


def _invalid() -> None:
    raise ServiceError("METADATA_INVALID", "항목 메타데이터 검증에 실패했습니다.")


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _integer(value) -> bool:
    return type(value) is int and value >= 0


def _day(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _https(value) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == "https" and bool(url.hostname) and not url.username
                and not url.password and url.port in (None, 443))
    except ValueError:
        return False


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


class Catalog:
    """All-or-nothing loader with copy-on-read records.

    ``load`` accepts a locally configured path. Its expected hash is optional so
    a newly reviewed dataset can replace an earlier population without changing
    code. No item counts, real IDs, or private paths are frozen into this module.
    """

    def __init__(self, payload: dict, dataset_hash: str):
        self._validate(payload)
        self._payload = deepcopy(payload)
        self.dataset_hash = dataset_hash
        self.is_synthetic = payload.get("dataset_kind") == "SYNTHETIC"
        self._items = {item["item_id"]: item for item in self._payload["items"]}
        self._sources = {source["source_id"]: source for source in self._payload["sources"]}

    @classmethod
    def load(cls, path, expected_hash=None) -> "Catalog":
        if expected_hash is not None and (
            not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash)
        ):
            raise ServiceError("METADATA_HASH_INVALID", "메타데이터 해시 설정이 올바르지 않습니다.")
        try:
            source = Path(path)
            if source.stat().st_size > 64 * 1024 * 1024:
                _invalid()
            raw = source.read_bytes()
        except (OSError, TypeError, ValueError):
            raise ServiceError("METADATA_UNAVAILABLE", "항목 메타데이터를 읽을 수 없습니다.", 503) from None
        digest = hashlib.sha256(raw).hexdigest()
        if expected_hash is not None and digest != expected_hash.lower():
            raise ServiceError("METADATA_HASH_MISMATCH", "항목 메타데이터 해시가 일치하지 않습니다.")
        try:
            payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object,
                                 parse_constant=lambda _: _invalid())
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            _invalid()
        return cls(payload, digest)

    @staticmethod
    def _validate(payload):
        if not isinstance(payload, dict) or payload.get("schema_version") != "1.1":
            _invalid()
        synthetic = payload.get("dataset_kind") == "SYNTHETIC"
        if payload.get("dataset_kind") not in (None, "REAL", "SYNTHETIC"):
            _invalid()
        if payload.get("external_document_body_included") is not False:
            _invalid()
        if payload.get("public_git_release_status") not in (_RELEASE_STATUS, "SYNTHETIC"):
            _invalid()
        if payload.get("content_review_status") not in ("PASS_FACT_FIELDS_ONLY", "SYNTHETIC"):
            _invalid()
        if not synthetic and (payload["public_git_release_status"] != _RELEASE_STATUS
                              or payload["content_review_status"] != "PASS_FACT_FIELDS_ONLY"):
            _invalid()
        items, sources = payload.get("items"), payload.get("sources")
        if not isinstance(items, list) or not isinstance(sources, list):
            _invalid()
        if not _integer(payload.get("item_count")) or payload["item_count"] != len(items):
            _invalid()
        counts = payload.get("counts_by_provider")
        if not isinstance(counts, dict) or any(not _text(k) or not _integer(v) for k, v in counts.items()):
            _invalid()
        source_map = {}
        for source in sources:
            if not isinstance(source, dict):
                _invalid()
            if any(not _text(source.get(k)) for k in ("source_id", "provider", "title", "url")):
                _invalid()
            if (source["source_id"] in source_map or not _https(source["url"])
                    or source.get("format") not in ("HTML", "PDF")
                    or not _day(source.get("checked_on"))
                    or not _integer(source.get("metadata_item_count"))
                    or source.get("source_body_included") is not False):
                _invalid()
            if synthetic != source["source_id"].startswith("SYNTHETIC-"):
                _invalid()
            source_map[source["source_id"]] = source
        seen = set()
        for item in items:
            if not isinstance(item, dict) or any(not _text(item.get(k)) for k in _ITEM_TEXT):
                _invalid()
            if any(k not in item or (item[k] is not None and not _text(item[k])) for k in _NULLABLE_TEXT):
                _invalid()
            if item["item_id"] in seen or synthetic != item["item_id"].startswith("SYNTHETIC-"):
                _invalid()
            seen.add(item["item_id"])
            for key in ("monthly_fee_won", "transfer_limit"):
                if key not in item or (item[key] is not None and not _integer(item[key])):
                    _invalid()
            if "transfer_allowed" not in item or (item["transfer_allowed"] is not None
                                                   and type(item["transfer_allowed"]) is not bool):
                _invalid()
            if (not _day(item["verified_on"]) or item["sale_status"] != "NOT_VERIFIED"
                    or item["verification_status"] != ("SYNTHETIC" if synthetic else _FACT_STATUS)):
                _invalid()
            source = source_map.get(item["source_id"])
            if (source is None or item["source_url"] != source["url"]
                    or item["provider"] != source["provider"]):
                _invalid()
        if Counter(item["provider"] for item in items) != counts:
            _invalid()
        source_counts = Counter(item["source_id"] for item in items)
        if any(source_counts[source_id] != source["metadata_item_count"]
               for source_id, source in source_map.items()):
            _invalid()

    @property
    def metadata(self) -> dict:
        return deepcopy({k: v for k, v in self._payload.items() if k not in ("items", "sources")})

    @property
    def sources(self) -> dict:
        return deepcopy(self._sources)

    def _record(self, item):
        record = deepcopy(item)
        record["readiness"] = {"status": "META_READY", "documents": "DOC_BLOCKED",
                               "reasons": ["NO_APPROVED_LINKED_DOCUMENTS"]}
        record["is_synthetic"] = self.is_synthetic
        return record

    @property
    def items(self) -> list[dict]:
        return [self._record(item) for item in self._items.values()]

    def get(self, item_id) -> dict:
        if not isinstance(item_id, str) or item_id not in self._items:
            raise ServiceError("ITEM_NOT_FOUND", "항목을 찾을 수 없습니다.", 404)
        return self._record(self._items[item_id])

    def summary(self) -> dict:
        return {
            "schema_version": self._payload["schema_version"],
            "dataset_hash": self.dataset_hash,
            "item_count": len(self._items),
            "real_item_count": 0 if self.is_synthetic else len(self._items),
            "synthetic_item_count": len(self._items) if self.is_synthetic else 0,
            "is_synthetic": self.is_synthetic,
            "counts_by_provider": deepcopy(self._payload["counts_by_provider"]),
            "source_count": len(self._sources),
            "official_document_body_count": 0,
            "external_document_body_included": False,
            "public_git_release_status": self._payload["public_git_release_status"],
            "content_review_status": self._payload["content_review_status"],
            "unknown_field_counts": {k: sum(item.get(k) is None for item in self._items.values())
                                     for k in ("monthly_fee_won", "product_id", "transfer_allowed", "transfer_limit")},
            "sale_status_counts": dict(Counter(item["sale_status"] for item in self._items.values())),
            "readiness": {"status": "META_READY", "documents": "DOC_BLOCKED",
                          "reasons": ["NO_APPROVED_LINKED_DOCUMENTS"]},
        }

    @staticmethod
    def _parse(query):
        conditions, unsupported = [], []
        residual = query.casefold()
        providers = (("SKT", r"(?<![a-z])skt(?![a-z])|에스케이(?:텔레콤)?"),
                     ("KT", r"(?<![a-z])kt(?![a-z])|케이티"),
                     ("삼성전자", r"삼성전자|삼성|samsung"),
                     ("Apple", r"애플|apple"))
        found = []
        for provider, pattern in providers:
            if re.search(pattern, residual):
                found.append(provider)
                residual = re.sub(pattern, " ", residual)
        if found:
            conditions.append({"field": "provider", "operator": "in", "value": found})
        # Remove unsupported contract conditions before parsing any money: a
        # coverage limit of 1 million won is not a monthly-fee constraint.
        unknown_patterns = (
            ("coverage_limit_won", r"(?:보장\s*한도|보상\s*한도|보장\s*금액)(?:\s*[\d,.]+\s*(?:만|천)?\s*원?\s*(?:이상|이하|미만|초과)?)?"),
            ("deductible_won", r"자기\s*부담금(?:\s*[\d,.]+\s*(?:만|천)?\s*원?\s*(?:이상|이하|미만|초과)?)?"),
            ("coverage_months", r"(?:보장|보증|가입)\s*기간(?:\s*\d+\s*(?:개월|년)\s*(?:이상|이하)?)?|\d+\s*(?:개월|년)\s*(?:이상|이하)?\s*(?:보장|보증)"),
            ("transfer_limit", r"(?:양도|이전)\s*\d+\s*회\s*(?:이상|이하)?|\d+\s*회\s*(?:이상|이하)?\s*(?:양도|이전)"),
            ("sale_status", r"(?:현재\s*)?(?:신규\s*)?(?:판매\s*중|가입\s*가능)(?:한)?"),
        )
        for field, pattern in unknown_patterns:
            if re.search(pattern, residual):
                unsupported.append(field)
                residual = re.sub(pattern, " ", residual)
        money = r"(?:월\s*(?:요금|이용료|보험료)?\s*)?(\d[\d,]*(?:\.\d+)?)\s*(만|천)?\s*원?\s*(이하|미만|이내|까지|이상|초과)"
        for match in list(re.finditer(money, residual)):
            amount = Decimal(match[1].replace(",", "")) * {None: 1, "만": 10000, "천": 1000}[match[2]]
            if amount != amount.to_integral_value():
                raise ServiceError("SEARCH_CONDITION_INVALID", "금액 조건은 원 단위 정수로 입력해 주세요.")
            conditions.append({"field": "monthly_fee_won", "operator": {"이하": "le", "미만": "lt", "이내": "le", "까지": "le", "이상": "ge", "초과": "gt"}[match[3]], "value": int(amount)})
        residual = re.sub(money, " ", residual)
        transfer = r"(?:양도|이전)\s*(불가능(?:한)?|불가(?:한)?|안\s*되\w*|가능(?:한)?|할\s*수\s*있\w*)|transfer(?:able)?"
        for match in re.finditer(transfer, residual):
            negative = bool(match[1] and re.match(r"불가|불가능|안", match[1]))
            conditions.append({"field": "transfer_allowed", "operator": "eq", "value": not negative})
        residual = re.sub(transfer, " ", residual)
        device = r"아이폰\s*\d*|iphone\s*\d*|폴드\s*\d*|fold\s*\d*|플립\s*\d*|flip\s*\d*|안드로이드|android|갤럭시"
        for match in re.finditer(device, residual):
            value = re.sub(r"\s+", "", match[0])
            for english, korean in (("iphone", "아이폰"), ("fold", "폴드"), ("flip", "플립"), ("android", "안드로이드")):
                value = value.replace(english, korean)
            conditions.append({"field": "device_category", "operator": "device", "value": value})
        residual = re.sub(device, " ", residual)
        residual = re.sub(r"(?:상품|보험|보증|조건|추천|찾아\w*|검색\w*|보여\w*|알려\w*|가능한|가능|이면서|이고|인|으로|이하|원|월|와|과|및|하고|해줘|주세요)", " ", residual)
        tokens = re.findall(r"[a-z0-9가-힣+]+", residual)
        return conditions, unsupported, tokens

    @staticmethod
    def _device(category, expected):
        category = re.sub(r"\s+", "", category.casefold())
        base = re.sub(r"\d+$", "", expected)
        if base in category:
            # "스마트폰(아이폰·폴더블 제외)" records exclusions, not support.
            if re.search(r"\([^)]*" + re.escape(base) + r"[^)]*제외", category):
                return False
            return True if expected in category else None
        if base in ("폴드", "플립") and "폴더블제외" in category:
            return False
        if base == "아이폰" and "안드로이드" in category:
            return False
        if base == "안드로이드" and ("아이폰" in category or "ios" in category):
            return False
        if base == "아이폰" and "ios" in category:
            return True if expected == base else None
        return None

    def search(self, query="", provider=None, max_fee=None, transfer_required=False) -> dict:
        if not isinstance(query, str) or len(query) > 2000:
            raise ServiceError("SEARCH_CONDITION_INVALID", "검색 조건을 확인해 주세요.")
        if provider is not None and not _text(provider):
            raise ServiceError("SEARCH_CONDITION_INVALID", "제공사 조건을 확인해 주세요.")
        if max_fee is not None and not _integer(max_fee):
            raise ServiceError("SEARCH_CONDITION_INVALID", "월요금은 0 이상의 정수로 입력해 주세요.")
        if type(transfer_required) is not bool:
            raise ServiceError("SEARCH_CONDITION_INVALID", "양도 조건을 확인해 주세요.")
        conditions, unsupported, tokens = self._parse(query)
        if provider is not None:
            conditions.append({"field": "provider", "operator": "eq", "value": provider})
        if max_fee is not None:
            conditions.append({"field": "monthly_fee_won", "operator": "le", "value": max_fee})
        if transfer_required:
            conditions.append({"field": "transfer_allowed", "operator": "eq", "value": True})
        matches, unknown = [], []
        for item in self._items.values():
            # Full fact text can mention an excluded condition. Keyword search is
            # discovery only; contract attributes are checked separately below.
            text = " ".join(str(item[k]) for k in ("name", "provider", "product_group", "device_category")).casefold()
            if any(token not in text for token in tokens):
                continue
            missing = list(unsupported)
            rejected = False
            for condition in conditions:
                field, operator, expected = condition["field"], condition["operator"], condition["value"]
                actual = item.get(field)
                if actual is None or (field == "sale_status" and actual == "NOT_VERIFIED"):
                    missing.append(field)
                    continue
                if operator == "device":
                    satisfied = self._device(actual, expected)
                    if satisfied is None:
                        missing.append(field)
                        continue
                else:
                    satisfied = {"eq": lambda: actual == expected, "in": lambda: actual in expected,
                                 "le": lambda: actual <= expected, "lt": lambda: actual < expected,
                                 "ge": lambda: actual >= expected, "gt": lambda: actual > expected}[operator]()
                if not satisfied:
                    rejected = True
                    break
            if rejected:
                continue
            record = self._record(item)
            if missing:
                record["unknown_conditions"] = sorted(set(missing))
                unknown.append(record)
            else:
                matches.append(record)
        return {"matches": matches, "unknown": unknown, "conditions": conditions,
                "unsupported_conditions": unsupported, "keywords": tokens,
                "is_synthetic": self.is_synthetic}

    @classmethod
    def synthetic(cls) -> "Catalog":
        """Two authored demo facts, never counted as real products or documents."""
        source = {"source_id": "SYNTHETIC-SOURCE", "provider": "SYNTHETIC",
                  "title": "SYNTHETIC demo reference (not official terms)",
                  "url": "https://example.invalid/synthetic", "format": "HTML",
                  "checked_on": "2026-01-01", "metadata_item_count": 2,
                  "source_body_included": False}
        items = []
        for index, fee in ((1, 4000), (2, None)):
            items.append({"item_id": f"SYNTHETIC-{index:03d}", "name": f"SYNTHETIC 시연 항목 {index}",
                          "provider": "SYNTHETIC", "product_group": "SYNTHETIC DEMO",
                          "source_offer_name": f"DEMO {index}", "device_category": "SYNTHETIC 스마트폰",
                          "monthly_fee_won": fee, "monthly_fee_basis": "SYNTHETIC authored demo amount",
                          "product_id": None, "source_group_product_id": None,
                          "source_id": source["source_id"], "source_url": source["url"],
                          "source_locator": "SYNTHETIC", "source_version_note": None,
                          "verified_on": "2026-01-01", "sale_status": "NOT_VERIFIED",
                          "transfer_allowed": None, "transfer_limit": None,
                          "verification_status": "SYNTHETIC",
                          "index_text": f"SYNTHETIC authored demo item {index}; transfer conditions unknown."})
        payload = {"schema_version": "1.1", "dataset_kind": "SYNTHETIC", "item_count": 2,
                   "counts_by_provider": {"SYNTHETIC": 2}, "sources": [source], "items": items,
                   "external_document_body_included": False, "content_review_status": "SYNTHETIC",
                   "public_git_release_status": "SYNTHETIC"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return cls(payload, digest)
