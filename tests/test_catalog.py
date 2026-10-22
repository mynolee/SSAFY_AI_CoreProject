"""Authored synthetic fixtures; no private input snapshots."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from warranty.catalog import Catalog
from warranty.errors import ServiceError


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.payload = deepcopy(Catalog.synthetic()._payload)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "metadata.json"

    def write(self, payload=None):
        raw = json.dumps(self.payload if payload is None else payload, ensure_ascii=False).encode("utf-8")
        self.path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def assert_invalid(self, payload=None):
        self.write(payload)
        with self.assertRaises(ServiceError) as raised:
            Catalog.load(self.path)
        self.assertEqual(raised.exception.code, "METADATA_INVALID")

    def search_catalog(self):
        payload = deepcopy(self.payload)
        payload["sources"][0]["provider"] = "SKT"
        payload["sources"][0]["metadata_item_count"] = 3
        first, second = payload["items"]
        third = deepcopy(first)
        third["item_id"] = "SYNTHETIC-003"
        third["name"] = "SYNTHETIC 세 번째"
        third["monthly_fee_won"] = 9000
        third["transfer_allowed"] = False
        third["device_category"] = "스마트폰(아이폰·폴더블 제외)"
        first["transfer_allowed"] = True
        first["device_category"] = "아이폰"
        second["device_category"] = "스마트폰"
        payload["items"].append(third)
        for item in payload["items"]:
            item["provider"] = "SKT"
        payload["item_count"] = 3
        payload["counts_by_provider"] = {"SKT": 3}
        self.write(payload)
        return Catalog.load(self.path)

    def test_hash_validation_and_preservation(self):
        digest = self.write()
        catalog = Catalog.load(self.path, digest.upper())
        self.assertEqual(catalog.dataset_hash, digest)
        for original, loaded in zip(self.payload["items"], catalog.items):
            self.assertEqual(original, {key: loaded[key] for key in original})
            self.assertEqual(loaded["readiness"]["documents"], "DOC_BLOCKED")
        self.assertIsNone(catalog.get("SYNTHETIC-002")["monthly_fee_won"])
        self.assertIsNone(catalog.get("SYNTHETIC-001")["transfer_allowed"])

    def test_hash_mismatch_is_not_a_fallback(self):
        self.write()
        with self.assertRaises(ServiceError) as raised:
            Catalog.load(self.path, "0" * 64)
        self.assertEqual(raised.exception.code, "METADATA_HASH_MISMATCH")

    def test_failures_do_not_reveal_private_paths(self):
        with self.assertRaises(ServiceError) as raised:
            Catalog.load(self.path / "not-there")
        self.assertEqual(raised.exception.code, "METADATA_UNAVAILABLE")
        self.assertNotIn(str(self.path), str(raised.exception.as_dict()))

    def test_duplicate_json_keys_and_nonfinite_numbers_fail(self):
        for raw in ('{"schema_version":"1.1","schema_version":"other"}', '{"item_count":NaN}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ServiceError) as raised:
                    Catalog.load(self.path)
                self.assertEqual(raised.exception.code, "METADATA_INVALID")

    def test_identity_count_and_provider_validation(self):
        for mutation in (
            lambda p: p["items"][1].update(item_id=p["items"][0]["item_id"]),
            lambda p: p.update(item_count=110),
            lambda p: p.update(counts_by_provider={"OTHER": 2}),
            lambda p: p["items"][0].update(item_id=""),
            lambda p: p["items"][0].pop("index_text"),
        ):
            with self.subTest(mutation=mutation):
                payload = deepcopy(self.payload)
                mutation(payload)
                self.assert_invalid(payload)

    def test_strict_nullable_types_and_known_statuses(self):
        for field, value in (("monthly_fee_won", True), ("monthly_fee_won", -1),
                             ("monthly_fee_won", "4000"), ("transfer_allowed", 0),
                             ("transfer_limit", False), ("product_id", 123),
                             ("sale_status", "AVAILABLE"), ("verification_status", "PASS"),
                             ("verified_on", "2026-02-30"), ("verified_on", "20260101")):
            with self.subTest(field=field, value=value):
                payload = deepcopy(self.payload)
                payload["items"][0][field] = value
                self.assert_invalid(payload)

    def test_sources_must_match_without_body_claims(self):
        for mutation in (
            lambda p: p["sources"].append(deepcopy(p["sources"][0])),
            lambda p: p["sources"][0].update(metadata_item_count=1),
            lambda p: p["sources"][0].update(source_body_included=True),
            lambda p: p.update(external_document_body_included=True),
            lambda p: p["items"][0].update(source_id="missing"),
            lambda p: p["items"][0].update(source_url="https://other.invalid/"),
            lambda p: p["sources"][0].update(url="http://example.invalid/"),
            lambda p: p["sources"][0].update(checked_on="not-a-date"),
        ):
            with self.subTest(mutation=mutation):
                payload = deepcopy(self.payload)
                mutation(payload)
                self.assert_invalid(payload)

    def test_future_population_not_frozen_to_current_count(self):
        payload = deepcopy(self.payload)
        payload["items"].pop()
        payload["item_count"] = 1
        payload["counts_by_provider"] = {"SYNTHETIC": 1}
        payload["sources"][0]["metadata_item_count"] = 1
        self.write(payload)
        self.assertEqual(Catalog.load(self.path).summary()["item_count"], 1)

    def test_copy_on_read_cannot_change_other_sessions(self):
        catalog = Catalog.synthetic()
        catalog.items[0]["name"] = "changed"
        catalog.get("SYNTHETIC-001")["monthly_fee_won"] = 0
        catalog.sources["SYNTHETIC-SOURCE"]["url"] = "changed"
        catalog.metadata["counts_by_provider"].clear()
        self.assertEqual(catalog.get("SYNTHETIC-001")["monthly_fee_won"], 4000)
        self.assertEqual(catalog.summary()["counts_by_provider"], {"SYNTHETIC": 2})
        with self.assertRaises(ServiceError) as raised:
            catalog.get("other")
        self.assertEqual(raised.exception.status, 404)

    def test_synthetic_fallback_never_counts_as_real(self):
        summary = Catalog.synthetic().summary()
        self.assertEqual(summary["real_item_count"], 0)
        self.assertEqual(summary["synthetic_item_count"], 2)
        self.assertEqual(summary["official_document_body_count"], 0)
        self.assertEqual(summary["public_git_release_status"], "SYNTHETIC")

    def test_null_budget_is_separate_from_confirmed_match(self):
        result = self.search_catalog().search(max_fee=5000)
        self.assertEqual([r["item_id"] for r in result["matches"]], ["SYNTHETIC-001"])
        self.assertEqual([r["item_id"] for r in result["unknown"]], ["SYNTHETIC-002"])
        self.assertEqual(result["unknown"][0]["unknown_conditions"], ["monthly_fee_won"])

    def test_transfer_false_is_rejected_and_null_stays_unknown(self):
        result = self.search_catalog().search(transfer_required=True)
        self.assertEqual([r["item_id"] for r in result["matches"]], ["SYNTHETIC-001"])
        self.assertEqual([r["item_id"] for r in result["unknown"]], ["SYNTHETIC-002"])

    def test_natural_language_conditions_are_checked_on_values(self):
        result = self.search_catalog().search("SKT 월 5천원 이하이고 양도 가능한 상품 추천")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(len(result["unknown"]), 1)
        self.assertEqual(result["keywords"], [])
        self.assertEqual(result["unknown"][0]["unknown_conditions"], ["monthly_fee_won", "transfer_allowed"])

    def test_strict_money_boundaries_and_decimal_units(self):
        catalog = self.search_catalog()
        self.assertEqual(len(catalog.search("월 0.4만원 이하")["matches"]), 1)
        self.assertEqual(len(catalog.search("월 4,000원 미만")["matches"]), 0)
        self.assertEqual(len(catalog.search("월 4,000원 초과")["matches"]), 1)
        self.assertEqual(len(catalog.search("월 1만원 이하")["matches"]), 2)

    def test_provider_name_does_not_confuse_kt_and_skt(self):
        catalog = self.search_catalog()
        self.assertEqual(len(catalog.search("SKT")["matches"]), 3)
        self.assertEqual(len(catalog.search("KT")["matches"]), 0)
        self.assertEqual(len(catalog.search("SKT", provider="KT")["matches"]), 0)

    def test_contract_limits_are_not_monthly_prices(self):
        result = self.search_catalog().search("보장 한도 100만원 이상")
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["unknown"]), 3)
        self.assertEqual(result["conditions"], [])
        self.assertEqual(result["unsupported_conditions"], ["coverage_limit_won"])

    def test_current_sale_status_never_becomes_availability(self):
        result = self.search_catalog().search("현재 신규 가입 가능한 상품")
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["unknown"]), 3)
        self.assertEqual(result["unknown"][0]["sale_status"], "NOT_VERIFIED")

    def test_device_exclusion_is_not_a_confirmed_match(self):
        result = self.search_catalog().search("아이폰")
        self.assertEqual([r["item_id"] for r in result["matches"]], ["SYNTHETIC-001"])
        self.assertEqual([r["item_id"] for r in result["unknown"]], ["SYNTHETIC-002"])

    def test_zero_matches_and_invalid_filter_inputs(self):
        self.assertEqual(self.search_catalog().search(max_fee=0)["matches"], [])
        for arguments in ({"max_fee": True}, {"max_fee": -1}, {"transfer_required": 1}, {"query": []}):
            with self.subTest(arguments=arguments), self.assertRaises(ServiceError):
                self.search_catalog().search(**arguments)


if __name__ == "__main__":
    unittest.main()
