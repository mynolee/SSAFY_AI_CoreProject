"""Public synthetic data and httpx.MockTransport only; never a provider call."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from warranty.errors import ServiceError
from warranty.gms import GMS_BASE_URL
from warranty.public_gms import GMSPublicStructuredModel
from warranty.public_protocol import PublicProtocol, public_instruction_hashes, public_source_sha256


QUESTION = "What does the fictional public transfer excerpt establish?"
QUESTION_TWO = "Which public step is stated for a fictional second device?"
SOURCE = {"local_id": "PRIVATE-LOCAL-ID", "title": "Fictional public terms",
          "url": "https://example.invalid/fictional-public-terms",
          "excerpt": "This fictional plan requires written confirmation for a transfer."}
OTHER_SOURCE = {**SOURCE, "title": "Fictional second public terms",
                "excerpt": "This fictional second plan requires a public transfer form."}
ENV = {"GMS_API_KEY": "public-test-dummy-never-live", "GMS_BASE_URL": GMS_BASE_URL,
       "GMS_CHAT_MODEL": "gpt-4.1-mini", "GMS_CHAT_ROUTE": "chat/completions",
       "GMS_MAX_OUTPUT_TOKENS": "128"}
APPROVAL = "public-synthetic-approval"


def manifest():
    return {"schema_version": 1, "model": "gpt-4.1-mini", "route": "chat/completions",
            "instruction_sha256s": public_instruction_hashes(),
            "question_sha256s": [hashlib.sha256(q.encode()).hexdigest() for q in (QUESTION, QUESTION_TWO)],
            "source_sha256s": [public_source_sha256(s) for s in (SOURCE, OTHER_SOURCE)],
            "max_request_bytes": 16384, "max_output_tokens": 128}


def variables(kind="query", question=QUESTION, *, local_id="PRIVATE-LOCAL-ID", number=7):
    value = {"item": {"item_id": "PRIVATE-CATALOG-SENTINEL", "name": "PRIVATE-ITEM-SENTINEL"},
             "question": question, "applicability": {"contract": "PRIVATE-SETTINGS-SENTINEL"}}
    if kind == "selection":
        value["candidates"] = [{"id": local_id, "title": SOURCE["title"], "role": "SERVICE_TERMS",
                                 "own_document": True, "private": "PRIVATE-CANDIDATE-SENTINEL"}]
    if kind == "answer":
        public = SOURCE if question == QUESTION else OTHER_SOURCE
        value.update(evidence=[
            {"evidence_id": "PRIVATE-METADATA-ID", "source_type": "metadata",
             "text": "PRIVATE-METADATA-SENTINEL", "number": 1},
            {"evidence_id": local_id, "source_type": "official_document", "number": number,
             "text": "PRIVATE-LOCAL-CONTEXT-SENTINEL " + public["excerpt"], "url": public["url"],
             "source_id": "PRIVATE-SOURCE-ID", "version_id": "PRIVATE-VERSION-ID"}],
            missing_information=["PRIVATE-MISSING-SENTINEL"], conflicts=["PRIVATE-CONFLICT-SENTINEL"])
    return value


def envelope(value):
    return json.dumps({"policy": {"template": "PRIVATE-POLICY-SENTINEL",
                                  "output_schema": "PRIVATE-SCHEMA-SENTINEL"}, "input": value})


class FakeUsage:
    ready = True

    def __init__(self, events, limit=100):
        self.events, self.limit, self.count = events, limit, 0
        self.lock = threading.Lock()

    def approval_matches(self, value):
        return value == APPROVAL

    def consume(self, kind, size):
        with self.lock:
            if self.count >= self.limit:
                raise ServiceError("USAGE_LIMIT_REACHED", "Synthetic limit", 429)
            self.count += 1
            self.events.append(("consume", kind, size))

    def snapshot(self):
        return {"reason_code": None}


class FakeCredit:
    ready = True

    def __init__(self, events):
        self.events, self.reservations, self.settlements = events, [], []
        self.on_reserve = None

    def approval_matches(self, value):
        return value == APPROVAL

    def estimate(self, kind, model, size, *, output_tokens_bound, route):
        self.events.append(("estimate", kind, size, output_tokens_bound, route))
        return {"estimated_upper_credits": "1.0", "input_tokens_bound": size + 1000,
                "output_tokens_bound": output_tokens_bound}

    def reserve(self, request_id, purpose, amount, **data):
        self.events.append(("reserve",))
        self.reservations.append({"request_id": request_id, "purpose": purpose, **data})
        if self.on_reserve:
            self.on_reserve()
        return {"replayed": False}

    def settle(self, request_id, actual_credits=None, *, usage=None):
        self.events.append(("settle",))
        self.settlements.append({"request_id": request_id, "actual_credits": actual_credits, "usage": usage})

    def allowed_reported_models(self, kind, model, route):
        return frozenset({model})

    def snapshot(self):
        return {"reason_code": None if self.ready else "LIVE_BUDGET_UNAVAILABLE"}

    def freeze(self, reason):
        self.ready = False
        self.events.append(("freeze", reason))


def response(content, **changes):
    value = {"model": "gpt-4.1-mini", "choices": [{"index": 0, "finish_reason": "stop",
             "message": {"role": "assistant", "content": content}}],
             "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}}
    value.update(changes)
    return value


class PublicGMSTests(unittest.TestCase):
    def setUp(self):
        self.events, self.requests, self.guard_records = [], [], []
        self.usage, self.credit = FakeUsage(self.events), FakeCredit(self.events)
        self.source, self.guard_allowed = deepcopy(SOURCE), True
        self.result, self.http_status = response('{"queries":["fictional transfer"]}'), 200
        self.adapters = []
        self.socket_guards = [patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
                              patch("socket.socket.connect_ex", side_effect=AssertionError("Network forbidden")),
                              patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
                              patch("socket.getaddrinfo", side_effect=AssertionError("Network forbidden"))]
        for guard in self.socket_guards:
            guard.start()
            self.addCleanup(guard.stop)
        self.addCleanup(self.close_models)

    def close_models(self):
        for model in self.adapters:
            model.close()

    def resolve(self, kind, value):
        return [] if kind == "query" else [deepcopy(self.source)]

    def guard(self, kind, context_kind, context):
        self.guard_records.append((kind, context_kind, deepcopy(context)))
        self.events.append(("guard",))
        return self.guard_allowed

    def transport(self, request):
        self.events.append(("http",))
        self.requests.append(request)
        return httpx.Response(self.http_status, json=self.result)

    def model(self, **changes):
        arguments = {"env": dict(ENV), "public_manifest": manifest(), "public_source_resolver": self.resolve,
                     "usage_budget": self.usage, "credit_budget": self.credit,
                     "transport": httpx.MockTransport(self.transport)}
        arguments.update(changes)
        model = GMSPublicStructuredModel(SimpleNamespace(remote_models=True), self.guard, **arguments)
        self.adapters.append(model)
        return model

    def call(self, model, kind="query", value=None):
        with model.live_execution("verification", APPROVAL):
            return model.generate(kind, envelope(value or variables(kind)))

    def assert_code(self, code, operation):
        with self.assertRaises(ServiceError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(ENV["GMS_API_KEY"], json.dumps(raised.exception.as_dict()))

    def test_exact_wire_only_public_projection_and_local_response_mappings(self):
        model = self.model()
        protocol = PublicProtocol(manifest())
        for kind, content in (("query", '{"queries":["fictional transfer"]}'),
                              ("selection", '{"selected":["S1"]}'),
                              ("answer", '{"answer":"Public explanation [S1]","citations":["S1"]}')):
            with self.subTest(kind=kind):
                value = variables(kind)
                self.result = response(content)
                result = self.call(model, kind, value)
                prepared = protocol.prepare(kind, value, self.resolve(kind, value))
                wire = self.requests[-1].content
                self.assertEqual(wire, prepared.wire_bytes)
                body = json.loads(wire)
                self.assertFalse(body["store"])
                self.assertFalse(body["stream"])
                self.assertEqual(body["max_tokens"], 128)
                self.assertNotIn("PRIVATE-", wire.decode())
                self.assertNotIn("policy", body)
                if kind == "selection":
                    self.assertEqual(result["selected_candidate_ids"], [SOURCE["local_id"]])
                if kind == "answer":
                    self.assertEqual(result["answer"], "Public explanation [7]")
                    self.assertEqual(result["cited_evidence_ids"], [SOURCE["local_id"]])
                    self.assertFalse(any(context.get("source_type") == "metadata"
                                         for _, _, context in self.guard_records))
        events = [row[0] for row in self.events]
        self.assertLess(events.index("consume"), events.index("reserve"))
        self.assertLess(events.index("reserve"), events.index("http"))
        self.assertEqual(len(self.credit.settlements), 3)
        self.assertTrue(all(row["actual_credits"] is None for row in self.credit.settlements))

    def test_no_scope_no_guard_or_unconfigured_environment_never_sends(self):
        model = self.model()
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", envelope(variables())))
        self.guard_allowed = False
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(model))
        class NoAmbientEnvironment:
            def get(self, *args):
                raise AssertionError("Ambient environment read forbidden")

        with patch("warranty.gms.os.environ", NoAmbientEnvironment()):
            empty = self.model(env=None)
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(empty))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.usage.count, 0)

    def test_unknown_exact_question_source_and_repair_envelope_block_before_reservation(self):
        model = self.model()
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(model, value=variables(question=QUESTION + " ")))
        self.source["excerpt"] += " unapproved mutation"
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(model, "selection"))
        with model.live_execution("verification", APPROVAL):
            invalid = json.loads(envelope(variables()))
            invalid["format_repair"] = {"attempt": 1}
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", json.dumps(invalid)))
        self.assertEqual(self.credit.reservations, [])
        self.assertEqual(self.requests, [])

    def test_guard_revocation_after_reservation_stops_http_and_retains_reservation(self):
        model = self.model()
        self.credit.on_reserve = lambda: setattr(self, "guard_allowed", False)
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(model))
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.credit.reservations), 1)
        self.assertEqual(self.credit.settlements[0]["usage"], None)

    def test_approved_but_changed_source_after_reservation_still_stops(self):
        model = self.model()
        self.credit.on_reserve = lambda: setattr(self, "source", deepcopy(OTHER_SOURCE))
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(model, "selection"))
        self.assertEqual(self.requests, [])
        self.assertEqual(len(self.credit.reservations), 1)

    def test_invalid_responses_are_single_request_with_no_format_repair(self):
        model = self.model()
        self.assertEqual(model.format_repair_attempts, 0)
        for result in (response('not JSON'), response('{"selected":["S99"]}'),
                       response('{"selected":["S1"]}', choices=[{"index": 0, "finish_reason": "length"}])):
            with self.subTest(result=result):
                self.result = result
                before = len(self.requests)
                self.assert_code("MODEL_OUTPUT_INVALID", lambda: self.call(model, "selection"))
                self.assertEqual(len(self.requests), before + 1)

    def test_answer_policy_requests_language_exact_markers_and_unverified_contract_default(self):
        model = self.model()
        self.result = response('{"answer":"An example procedure is described. [S1]","citations":["S1"]}')
        result = self.call(model, "answer")
        body = json.loads(self.requests[-1].content)
        instruction = body["messages"][0]["content"]
        for policy in ("same language as the question", "exact bracket form [S1]",
                       "citations array must exactly match", "Prefer omitting classification",
                       "actual contract scope or application is unverified", "Hypothetical question",
                       "publication or effective dates", "never more than 512 tokens"):
            self.assertIn(policy, instruction)
        self.assertEqual(body["max_tokens"], 128)
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(model.format_repair_attempts, 0)

    def test_parenthetical_answer_citation_fails_once_without_repair(self):
        model = self.model()
        self.result = response('{"answer":"An example procedure (S1)","citations":["S1"]}')
        self.assert_code("MODEL_OUTPUT_INVALID", lambda: self.call(model, "answer"))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.credit.reservations), 1)
        self.assertEqual(len(self.credit.settlements), 1)
        self.assertEqual(model.format_repair_attempts, 0)

    def test_http_failure_does_not_retry_and_unknown_model_freezes_shared_credit(self):
        model = self.model()
        self.http_status = 503
        self.assert_code("MODEL_UNAVAILABLE", lambda: self.call(model))
        self.assertEqual(len(self.requests), 1)
        self.http_status = 200
        self.result = response('{"queries":[]}', model="unapproved-provider-model")
        self.assert_code("LIVE_MODEL_IDENTITY_MISMATCH", lambda: self.call(model))
        self.assertFalse(self.credit.ready)
        self.assert_code("LIVE_BUDGET_UNAVAILABLE", lambda: self.call(model))
        self.assertEqual(len(self.requests), 2)

    def test_request_limit_output_cap_and_public_cache_partition(self):
        model = self.model()
        self.usage.limit = 1
        self.call(model)
        self.assert_code("USAGE_LIMIT_REACHED", lambda: self.call(model))
        self.assertEqual(len(self.requests), 1)
        bad = self.model(max_tokens=129)
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: self.call(bad))
        declared = manifest()
        other = self.model(public_manifest={**declared, "max_request_bytes": 16383})
        self.assertNotEqual(model.cache_identity, other.cache_identity)
        self.assertTrue(model.cache_identity.startswith("gms-public-v1:"))
        self.assertTrue(model.public_protocol_only)

    def test_concurrent_requests_keep_question_source_and_local_alias_context_separate(self):
        barrier = threading.Barrier(2)

        def resolve(kind, value):
            second = value["question"] == QUESTION_TWO
            return [{**(OTHER_SOURCE if second else SOURCE), "local_id": "PRIVATE-B" if second else "PRIVATE-A"}]

        def transport(request):
            user = json.loads(json.loads(request.content)["messages"][1]["content"])
            barrier.wait(timeout=5)
            self.assertEqual(user["sources"][0]["excerpt"],
                             OTHER_SOURCE["excerpt"] if user["question"] == QUESTION_TWO else SOURCE["excerpt"])
            return httpx.Response(200, json=response('{"answer":"Public concurrent answer [S1]","citations":["S1"]}'))

        model = self.model(public_source_resolver=resolve, transport=httpx.MockTransport(transport))
        rows = [(QUESTION, "PRIVATE-A", 7), (QUESTION_TWO, "PRIVATE-B", 9)]

        def run(row):
            question, identity, number = row
            return self.call(model, "answer", variables("answer", question, local_id=identity, number=number))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, rows))
        self.assertEqual([result["cited_evidence_ids"] for result in results], [["PRIVATE-A"], ["PRIVATE-B"]])
        self.assertEqual([result["answer"] for result in results],
                         ["Public concurrent answer [7]", "Public concurrent answer [9]"])
        self.assertEqual(len(self.credit.reservations), 2)
        self.assertEqual(len({row["request_id"] for row in self.credit.reservations}), 2)


if __name__ == "__main__":
    unittest.main()
