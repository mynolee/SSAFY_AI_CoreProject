"""Invented credit profiles exercise accounting only; no actual prices or HTTP."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.live_budget import CreditBudget, PURPOSES


FINGERPRINT = hashlib.sha256(b"SYNTHETIC LIVE context not provider approval").hexdigest()


def synthetic_profile(*, allocations=None, demo_reserve="10000", input_rate=100, output_rate=200):
    """Deliberately invented test arithmetic; never a pricing reference."""
    common = {"input_bound": "wire_utf8_bytes", "input_bound_confirmed": True,
              "input_token_overhead": 0, "max_input_tokens": 100000,
              "output_bound_confirmed": True}
    return {"profile_id": "SYNTHETIC-profile-v1", "pricing_confirmed": True,
            "pricing_version": "SYNTHETIC-NOT-REAL-PRICES",
            "allocations": allocations or {"evaluation": "10000", "verification": "10000",
                "final_verification": "10000", "demo": "30000", "embedding_preparation": "10000"},
            "demo_reserve_credits": demo_reserve,
            "models": {"SYNTHETIC-chat": {**common, "kind": "chat", "route": "chat.completions",
                "input_microcredits_per_token": input_rate, "output_microcredits_per_token": output_rate,
                "output_bound": "max_tokens", "max_output_tokens": 100},
                "SYNTHETIC-embedding": {**common, "kind": "embedding", "route": "embeddings",
                "input_microcredits_per_token": input_rate, "output_microcredits_per_token": 0,
                "output_bound": "none", "max_output_tokens": 0}}}


def process_reserve(arguments):
    path, profile, index = arguments
    budget = CreditBudget(path, "SYNTHETIC-approval", profile, context_fingerprint=FINGERPRINT)
    try:
        plan = budget.estimate("chat", "SYNTHETIC-chat", 1, output_tokens_bound=1, route="chat.completions")
        budget.reserve("process-" + str(index), "evaluation", **plan)
        return "ACCEPTED"
    except ServiceError as error:
        return error.code


class CreditBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-live-budget-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "model_credits.sqlite3"

    def budget(self, profile=None, approval="SYNTHETIC-approval", fingerprint=FINGERPRINT):
        return CreditBudget(self.path, approval, profile, context_fingerprint=fingerprint)

    def approved(self, **changes):
        return self.budget(synthetic_profile(**changes))

    def reserve(self, budget, request_id="request-1", *, purpose="evaluation", size=4, output=2):
        plan = budget.estimate("chat", "SYNTHETIC-chat", size, output_tokens_bound=output, route="chat.completions")
        return budget.reserve(request_id, purpose, **plan)

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.path), caught.exception.message)
        return caught.exception

    def test_missing_or_unconfirmed_profile_denies_without_creating_files(self):
        incomplete = synthetic_profile()
        incomplete["pricing_confirmed"] = False
        for profile in (None, incomplete):
            budget = self.budget(profile)
            self.assertFalse(budget.confirmed)
            self.assertFalse(budget.ready)
            self.assertFalse(budget.approval_matches("SYNTHETIC-approval"))
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.estimate("chat", "SYNTHETIC-chat", 1, output_tokens_bound=1, route="chat.completions"))
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.settle("request"))
        self.assertFalse(self.path.parent.exists())

    def test_verified_profile_readiness_snapshot_and_identity_are_read_only(self):
        budget = self.approved()
        self.assertTrue(budget.confirmed and budget.ready)
        self.assertTrue(budget.approval_matches("SYNTHETIC-approval"))
        self.assertFalse(budget.approval_matches("another-approval"))
        state = budget.snapshot()
        self.assertEqual(state["global_limit_credits"], 70000)
        self.assertEqual(state["reserved_upper_credits"], "0.000000")
        self.assertIsNone(state["provider_reported_actual_credits"])
        for private in (str(self.path), "SYNTHETIC-approval", FINGERPRINT):
            self.assertNotIn(private, json.dumps(state))
        self.assertFalse(self.path.parent.exists())

    def test_estimate_is_exact_microcredits_and_has_confirmed_token_bounds(self):
        profile = synthetic_profile()
        profile["models"]["SYNTHETIC-chat"]["input_token_overhead"] = 3
        budget = self.budget(profile)
        plan = budget.estimate("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        self.assertEqual(plan["input_tokens_bound"], 7)
        self.assertEqual(plan["output_tokens_bound"], 2)
        self.assertEqual(plan["estimated_upper_credits"], "0.001100")
        embedding = budget.estimate("embedding", "SYNTHETIC-embedding", 7, route="embeddings")
        self.assertEqual((embedding["output_tokens_bound"], embedding["estimated_upper_credits"]), (0, "0.000700"))
        self.assertFalse(self.path.exists())

    def test_existing_empty_or_schema_less_store_is_never_initialized(self):
        self.path.parent.mkdir(parents=True)
        for schema_less in (False, True):
            with self.subTest(schema_less=schema_less):
                self.path.write_bytes(b"")
                if schema_less:
                    with sqlite3.connect(self.path) as connection:
                        connection.execute("PRAGMA user_version=0")
                    connection.close()
                before = self.path.read_bytes()
                budget = self.approved()
                plan = budget._plan("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
                self.assertFalse(budget.ready)
                self.assertFalse(budget.approval_matches("SYNTHETIC-approval"))
                self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: budget.reserve("blocked", "evaluation", **plan))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_truncated_or_unversioned_credit_store_cannot_reset_global_reservations(self):
        budget = self.approved()
        plan = budget.estimate("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        budget.reserve("first", "evaluation", **plan)
        original = self.path.read_bytes()
        for mutation in ("unversioned", "truncated"):
            with self.subTest(mutation=mutation):
                self.path.write_bytes(original)
                if mutation == "truncated":
                    self.path.write_bytes(b"")
                else:
                    with sqlite3.connect(self.path) as connection:
                        connection.execute("PRAGMA user_version=0")
                    connection.close()
                before = self.path.read_bytes()
                for reopened in (budget, self.approved(), self.budget(synthetic_profile(), approval="new-ID")):
                    self.assertFalse(reopened.ready)
                    self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: reopened.reserve("second", "verification", **plan))
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(original)
        self.assertEqual(self.approved().snapshot()["reserved_upper_credits"], "0.000800")

    def test_interrupted_bootstrap_is_not_deleted_or_automatically_repaired(self):
        self.path.parent.mkdir(parents=True)
        lock = self.path.with_name(self.path.name + ".initializing")
        lock.write_bytes(b"")
        budget = self.approved()
        plan = budget._plan("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        with patch("warranty.usage.time.monotonic", side_effect=[0, 3, 0, 3]):
            self.assertFalse(budget.ready)
            self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: budget.reserve("blocked", "evaluation", **plan))
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [lock])

    def test_reservation_replay_is_idempotent_and_conflicting_payload_is_denied(self):
        budget = self.approved()
        plan = budget.estimate("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        first = budget.reserve("same-request", "evaluation", **plan)
        replay = budget.reserve("same-request", "evaluation", **plan)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(budget.snapshot()["request_count"], 1)
        self.assert_code("LIVE_REQUEST_CONFLICT", lambda: budget.reserve("same-request", "verification", **plan))
        self.assertEqual(budget.snapshot()["reserved_upper_credits"], "0.000800")

    def test_concurrent_same_request_charges_once(self):
        budget = self.approved()
        plan = budget.estimate("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(lambda _: budget.reserve("same-request", "evaluation", **plan), range(20)))
        self.assertEqual(sum(not row["replayed"] for row in results), 1)
        self.assertEqual(budget.snapshot()["request_count"], 1)

    def test_all_approval_ids_and_purposes_share_the_70000_global_cap(self):
        allocations = {key: "0" for key in PURPOSES}
        allocations.update(evaluation="40000", verification="20000", demo="10000")
        profile = synthetic_profile(allocations=allocations, demo_reserve="10000", input_rate=1_000_000_000, output_rate=0)
        first = self.budget(profile, approval="approval-A")
        self.reserve(first, "evaluation", size=40, output=1)
        second = self.budget(profile, approval="approval-B")
        self.reserve(second, "verification", purpose="verification", size=20, output=1)
        self.assertEqual(first.snapshot()["reserved_upper_credits"], "60000.000000")
        self.assertEqual(first.snapshot()["demo_preserved_credits"], "10000.000000")
        self.reserve(second, "demo", purpose="demo", size=10, output=1)
        new_approval = self.budget(profile, approval="approval-C")
        self.assertEqual(new_approval.snapshot()["reserved_upper_credits"], "70000.000000")
        self.assertFalse(new_approval.ready)
        plan = first._plan("chat", "SYNTHETIC-chat", 1, output_tokens_bound=1, route="chat.completions")
        self.assert_code("CREDIT_LIMIT_REACHED", lambda: new_approval.reserve("new-ID-cannot-reset", "evaluation", **plan))
        self.assertEqual(new_approval.snapshot()["request_count"], 3)

    def test_purpose_allocation_and_demo_preservation_survive_restart(self):
        allocations = {key: "0" for key in PURPOSES}
        allocations.update(evaluation="0.001600", demo="0.001000")
        profile = synthetic_profile(allocations=allocations, demo_reserve="0.001000")
        first = self.budget(profile)
        self.reserve(first, "first")
        self.reserve(first, "second")
        reopened = self.budget(profile)
        self.assertEqual(reopened.snapshot()["purposes"]["evaluation"]["remaining_credits"], "0.000000")
        self.assertEqual(reopened.snapshot()["demo_preserved_credits"], "0.001000")
        self.assert_code("CREDIT_LIMIT_REACHED", lambda: self.reserve(reopened, "third"))
        self.reserve(reopened, "demo", purpose="demo")
        self.assertEqual(reopened.snapshot()["demo_preserved_credits"], "0.000200")

    def test_concurrent_processes_cannot_overspend_purpose_allocation(self):
        allocations = {key: "0" for key in PURPOSES}
        allocations.update(evaluation="10000", demo="60000")
        profile = synthetic_profile(allocations=allocations, demo_reserve="60000", input_rate=1_000_000_000, output_rate=0)
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as executor:
            results = list(executor.map(process_reserve, [(str(self.path), profile, index) for index in range(20)]))
        self.assertEqual(results.count("ACCEPTED"), 10)
        self.assertEqual(results.count("CREDIT_LIMIT_REACHED"), 10)
        self.assertEqual(self.budget(profile).snapshot()["reserved_upper_credits"], "10000.000000")

    def test_failed_or_unknown_result_keeps_reservation_and_does_not_claim_actual_billing(self):
        budget = self.approved()
        self.reserve(budget)
        settled = budget.settle("request-1", usage={"input_tokens": 2, "output_tokens": 1})
        self.assertEqual(settled["state"], "UNKNOWN")
        self.assertIsNone(settled["provider_reported_actual_credits"])
        self.assertEqual(settled["token_based_estimate_credits"], "0.000400")
        budget.settle("request-1", usage={"input_tokens": 2, "output_tokens": 1})
        state = self.budget(synthetic_profile()).snapshot()
        self.assertEqual(state["reserved_upper_credits"], "0.000800")
        self.assertEqual(state["unknown_request_count"], 1)
        self.assertIsNone(state["provider_reported_actual_credits"])

    def test_provider_credit_and_token_estimate_remain_distinct_and_never_refund(self):
        budget = self.approved()
        self.reserve(budget)
        self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.settle("request-1", "0.000600"))
        result = budget.settle("request-1", "0.000600", usage={"input_tokens": 2, "output_tokens": 1}, actual_credits_source="provider_reported")
        self.assertEqual(result["provider_reported_actual_credits"], "0.000600")
        self.assertEqual(result["token_based_estimate_credits"], "0.000400")
        self.assertEqual(budget.snapshot()["reserved_upper_credits"], "0.000800")
        self.assertEqual(budget.snapshot()["provider_reported_actual_credits"], "0.000600")
        budget.settle("request-1", "0.000600", actual_credits_source="provider_reported")
        self.assert_code("LIVE_REQUEST_CONFLICT", lambda: budget.settle("request-1", "0.000500", actual_credits_source="provider_reported"))

    def test_token_bound_violation_freezes_even_without_actual_credit_information(self):
        budget = self.approved()
        self.reserve(budget)
        result = budget.settle("request-1", usage={"input_tokens": 5, "output_tokens": 1})
        self.assertTrue(result["profile_mismatch"])
        self.assertFalse(budget.ready)
        self.assertFalse(self.budget(synthetic_profile(), approval="new-ID").ready)
        self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: self.reserve(budget, "blocked"))
        self.assertIsNone(budget.snapshot()["provider_reported_actual_credits"])

    def test_actual_credit_above_reserved_upper_bound_is_recorded_then_freezes(self):
        budget = self.approved()
        self.reserve(budget)
        result = budget.settle("request-1", "1.0", actual_credits_source="provider_reported")
        self.assertTrue(result["profile_mismatch"])
        self.assertEqual(budget.snapshot()["provider_reported_actual_credits"], "1.000000")
        self.assertFalse(budget.ready)

    def test_model_identity_allowlist_is_exact_and_freeze_is_persistent(self):
        profile = synthetic_profile()
        profile["models"]["SYNTHETIC-chat"]["allowed_reported_models"] = ["SYNTHETIC-chat-reviewed-version"]
        budget = self.budget(profile)
        self.assertEqual(budget.allowed_reported_models("chat", "SYNTHETIC-chat", "chat.completions"),
            frozenset({"SYNTHETIC-chat", "SYNTHETIC-chat-reviewed-version"}))
        self.assertEqual(budget.allowed_reported_models("chat", "invented-model", "chat.completions"), frozenset())
        self.reserve(budget)
        budget.freeze()
        self.assertFalse(budget.ready)
        self.assertFalse(self.budget(profile, approval="another-ID").ready)
        budget.settle("request-1", usage={"input_tokens": 1, "output_tokens": 0})
        self.assertEqual(self.budget(profile).snapshot()["reserved_upper_credits"], "0.000800")

    def test_changed_profile_context_or_project_allocations_cannot_reset_spend(self):
        profile = synthetic_profile()
        budget = self.budget(profile)
        self.reserve(budget)
        changed = deepcopy(profile)
        changed["models"]["SYNTHETIC-chat"]["input_microcredits_per_token"] += 1
        for replacement in (self.budget(changed), self.budget(profile, fingerprint="a" * 64)):
            self.assertFalse(replacement.ready)
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: self.reserve(replacement, "invalid"))
        changed_policy = deepcopy(profile)
        changed_policy["demo_reserve_credits"] = "10001"
        new_id = self.budget(changed_policy, approval="new-ID")
        self.assertFalse(new_id.ready)
        self.assertEqual(budget.snapshot()["reserved_upper_credits"], "0.000800")

    def test_unconfirmed_tokenizer_or_output_bound_and_wrong_route_are_denied(self):
        for field in ("input_bound_confirmed", "output_bound_confirmed"):
            profile = synthetic_profile()
            profile["models"]["SYNTHETIC-chat"][field] = False
            self.assertFalse(self.budget(profile).confirmed)
        budget = self.approved()
        for output in (None, 0, True, 101):
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.estimate("chat", "SYNTHETIC-chat", 1, output_tokens_bound=output, route="chat.completions"))
        self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.estimate("chat", "SYNTHETIC-chat", 1, output_tokens_bound=1, route="responses"))
        self.assertFalse(self.path.exists())

    def test_responses_uses_full_confirmed_model_output_bound_without_inventing_wire_field(self):
        profile = synthetic_profile()
        row = profile["models"]["SYNTHETIC-chat"]
        row.update(route="responses", output_bound="model_maximum", max_output_tokens=100)
        budget = self.budget(profile)
        plan = budget.estimate("chat", "SYNTHETIC-chat", 1, route="responses")
        self.assertEqual(plan["output_tokens_bound"], 100)
        self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.estimate("chat", "SYNTHETIC-chat", 1, output_tokens_bound=2, route="responses"))
        row["output_bound_confirmed"] = False
        self.assertFalse(self.budget(profile).ready)

    def test_reserve_recomputes_estimate_and_rejects_forged_smaller_bounds(self):
        budget = self.approved()
        plan = budget.estimate("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        for altered in ({**plan, "estimated_upper_credits": "0.000001"}, {**plan, "input_tokens_bound": 1},
                        {**plan, "input_tokens_bound": True}):
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: budget.reserve("forged", "evaluation", **altered))
        self.assert_code("LIVE_PURPOSE_REQUIRED", lambda: budget.reserve("forged", "validation", **plan))
        self.assertFalse(self.path.exists())

    def test_unknown_commit_keeps_global_reservation_and_replay_never_charges_again(self):
        class UncertainCommit(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("SYNTHETIC uncertain commit")
        budget = self.approved()
        plan = budget.estimate("chat", "SYNTHETIC-chat", 4, output_tokens_bound=2, route="chat.completions")
        def connect(read_only):
            self.assertFalse(read_only)
            connection = sqlite3.connect(self.path, isolation_level=None, factory=UncertainCommit)
            connection.row_factory = sqlite3.Row
            return connection
        with patch.object(budget, "_connect", side_effect=connect):
            self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: budget.reserve("uncertain", "evaluation", **plan))
        reopened = self.approved()
        self.assertEqual(reopened.snapshot()["reserved_upper_credits"], "0.000800")
        self.assertTrue(reopened.reserve("uncertain", "evaluation", **plan)["replayed"])
        self.assertEqual(reopened.snapshot()["request_count"], 1)

    def test_corrupt_storage_and_counter_inconsistency_fail_closed(self):
        budget = self.approved()
        self.reserve(budget)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE credit_project SET reserved_upper_micro=0")
        connection.close()
        self.assertFalse(budget.ready)
        self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: self.reserve(budget, "blocked"))

    def test_strict_caps_precision_and_profile_configuration_never_infer_prices(self):
        for value in (True, 1.1, "NaN", "Infinity", "0.0000001"):
            profile = synthetic_profile()
            profile["demo_reserve_credits"] = value
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: self.budget(profile))
        profile = synthetic_profile()
        profile["allocations"]["evaluation"] = "70001"
        self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: self.budget(profile))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
