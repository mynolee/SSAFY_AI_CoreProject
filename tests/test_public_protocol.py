"""Synthetic-only tests for the independent public wire and local remapping."""
from copy import deepcopy
import hashlib
import json
import unittest

from warranty.public_protocol import (MAX_REQUEST_BYTES, MAX_OUTPUT_TOKENS, PUBLIC_INSTRUCTIONS,
                                      PublicProtocol, public_instruction_hashes, public_source_sha256)
from warranty.prompts import validate_output


QUESTION = "For a fictional device, which transfer step is described?"
SOURCE = {"local_id": "private-evidence-123", "title": "Fictional public transfer example",
          "url": "https://example.invalid/transfer", "excerpt": "A fictional transfer needs written confirmation."}


def manifest():
    return {"schema_version": 1, "model": "gpt-4.1-mini", "route": "chat/completions",
            "instruction_sha256s": public_instruction_hashes(),
            "question_sha256s": [hashlib.sha256(QUESTION.encode("utf-8")).hexdigest()],
            "source_sha256s": [public_source_sha256(SOURCE)],
            "max_request_bytes": 4096, "max_output_tokens": 128}


def variables(kind):
    result = {"item": {"item_id": "private-item", "index_text": "PRIVATE_CATALOG_SENTINEL"},
              "question": QUESTION, "applicability": {"country": "PRIVATE_APPLICABILITY_SENTINEL"}}
    if kind == "selection":
        result["candidates"] = [{"id": SOURCE["local_id"], "title": "PRIVATE_CANDIDATE_CONTEXT"}]
    elif kind == "answer":
        result.update(evidence=[{"evidence_id": SOURCE["local_id"], "text": SOURCE["excerpt"], "number": 7,
                                 "url": SOURCE["url"], "source_type": "official_document"},
                                {"evidence_id": "private-meta", "text": "PRIVATE_METADATA_SENTINEL", "source_type": "metadata"}],
                      missing_information=["PRIVATE_MISSING_SENTINEL"], conflicts=["PRIVATE_CONFLICT_SENTINEL"])
    return result


class PublicProtocolTests(unittest.TestCase):
    def test_each_wire_is_public_exact_and_sealed(self):
        protocol = PublicProtocol(manifest())
        for kind in ("query", "selection", "answer"):
            with self.subTest(kind=kind):
                prepared = protocol.prepare(kind, variables(kind), [SOURCE])
                wire = prepared.wire_bytes.decode("utf-8")
                for sentinel in ("PRIVATE_", "private-evidence-123", "private-item", "private-meta",
                                 "item_id", "evidence_id", "missing_information", "conflicts", "output_schema"):
                    self.assertNotIn(sentinel, wire)
                body = prepared.body
                self.assertEqual(set(body), {"model", "messages", "max_tokens", "store", "stream"})
                self.assertIs(body["store"], False)
                self.assertIs(body["stream"], False)
                self.assertEqual(body["max_tokens"], 128)
                self.assertEqual(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(),
                                 prepared.wire_bytes)
                body["store"] = True
                self.assertIs(prepared.body["store"], False)
                with self.assertRaises(TypeError):
                    prepared.label_to_local_id["S1"] = "changed"
                self.assertEqual(prepared.label_to_local_id["S1"], SOURCE["local_id"])

    def test_public_replies_map_back_to_existing_local_contracts(self):
        protocol = PublicProtocol(manifest())
        replies = {"query": {"queries": ["fictional transfer"]}, "selection": {"selected": ["S1"]},
                   "answer": {"answer": "Written confirmation is described. [S1]", "citations": ["S1"]}}
        for kind, reply in replies.items():
            with self.subTest(kind=kind):
                prepared = protocol.prepare(kind, variables(kind), [SOURCE])
                result = protocol.decode(prepared, json.dumps(reply))
                validate_output(kind, result, allowed_candidate_ids=[SOURCE["local_id"]],
                                allowed_evidence_ids=[SOURCE["local_id"]])
                if kind == "answer":
                    self.assertEqual(result["classification"], "NEEDS_INFORMATION")
                    self.assertEqual(result["cited_evidence_ids"], [SOURCE["local_id"]])
                    self.assertIn("[7]", result["answer"])
                    self.assertNotIn("[S1]", result["answer"])

    def test_unknown_hashes_extra_fields_and_caps_fail_closed(self):
        protocol = PublicProtocol(manifest())
        changed = variables("query")
        changed["question"] += " "
        with self.assertRaisesRegex(ValueError, "QUESTION_HASH_UNKNOWN"):
            protocol.prepare("query", changed, [SOURCE])
        source = {**SOURCE, "excerpt": SOURCE["excerpt"] + " changed"}
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_UNKNOWN"):
            protocol.prepare("query", variables("query"), [source])
        with self.assertRaisesRegex(ValueError, "SOURCE_FIELDS_INVALID"):
            protocol.prepare("query", variables("query"), [{**SOURCE, "reference_answer": "private"}])
        for field, value in (("model", "another-model"), ("route", "responses"),
                             ("max_request_bytes", MAX_REQUEST_BYTES + 1), ("max_output_tokens", True)):
            with self.subTest(field=field):
                config = manifest()
                config[field] = value
                with self.assertRaises(ValueError):
                    PublicProtocol(config)
        config = manifest()
        config["instruction_sha256s"]["query"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "INSTRUCTION_HASH_UNKNOWN"):
            PublicProtocol(config)
        config = manifest()
        config["max_request_bytes"] = 1
        with self.assertRaisesRegex(ValueError, "BYTE_CAP_EXCEEDED"):
            PublicProtocol(config).prepare("query", variables("query"), [SOURCE])

    def test_context_mapping_and_evidence_binding_are_required(self):
        protocol = PublicProtocol(manifest())
        with self.assertRaisesRegex(ValueError, "MAPPING_INVALID"):
            protocol.prepare("selection", variables("selection"), [{**SOURCE, "local_id": "unregistered"}])
        with self.assertRaisesRegex(ValueError, "MAPPING_INCOMPLETE"):
            protocol.prepare("answer", variables("answer"), [])
        changed = variables("answer")
        changed["evidence"][0]["url"] = "https://example.invalid/different"
        with self.assertRaisesRegex(ValueError, "EVIDENCE_BINDING_INVALID"):
            protocol.prepare("answer", changed, [SOURCE])
        changed = variables("answer")
        changed["evidence"] = []
        prepared = protocol.prepare("answer", changed, [])
        result = protocol.decode(prepared, '{"answer":"The excerpts do not establish an answer.","citations":[]}')
        validate_output("answer", result, allowed_evidence_ids=[])
        self.assertEqual(result["claims"], [])

    def test_reply_labels_shape_and_duplicates_are_never_repaired(self):
        protocol = PublicProtocol(manifest())
        prepared = protocol.prepare("answer", variables("answer"), [SOURCE])
        for reply in ('{"answer":"x","citations":["private-evidence-123"]}',
                      '{"answer":"x","citations":["S2"]}',
                      '{"answer":"x","citations":["S1","S1"]}',
                      '{"answer":"x [7]","citations":["S1"]}',
                      '{"answer":"x","citations":["S1"]}',
                      '{"answer":"x","answer":"y","citations":[]}',
                      '{"answer":"x","citations":[],"classification":"TRANSFERABLE"}'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                protocol.decode(prepared, reply)
        config = manifest()
        config["max_output_tokens"] = 64
        with self.assertRaisesRegex(ValueError, "REQUEST_BINDING_INVALID"):
            PublicProtocol(config).decode(prepared, '{"answer":"x","citations":[]}')

    def test_optional_classification_is_a_grounded_candidate_with_a_safe_default(self):
        protocol = PublicProtocol(manifest())
        prepared = protocol.prepare("answer", variables("answer"), [SOURCE])
        base = {"answer": "Written confirmation is described. [S1]", "citations": ["S1"]}
        self.assertEqual(protocol.decode(prepared, json.dumps(base))["classification"], "NEEDS_INFORMATION")
        for classification in ("TRANSFERABLE", "NOT_TRANSFERABLE", "CONDITIONAL", "NEEDS_INFORMATION"):
            with self.subTest(classification=classification):
                result = protocol.decode(prepared, json.dumps({**base, "classification": classification}))
                self.assertEqual(result["classification"], classification)
                self.assertEqual(result["cited_evidence_ids"], [SOURCE["local_id"]])
                self.assertIn("[7]", result["answer"])
                validate_output("answer", result, allowed_evidence_ids=[SOURCE["local_id"]])
        for invalid in ("UNKNOWN", None, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "CLASSIFICATION_INVALID"):
                protocol.decode(prepared, json.dumps({**base, "classification": invalid}))
        for classification in ("TRANSFERABLE", "NOT_TRANSFERABLE", "CONDITIONAL"):
            with self.subTest(ungrounded=classification), self.assertRaisesRegex(ValueError, "CLASSIFICATION_INVALID"):
                protocol.decode(prepared, json.dumps({"answer": "No supported conclusion.", "citations": [],
                                                      "classification": classification}))

    def test_question_language_reply_keeps_safe_default_and_exact_local_citations(self):
        question = "가상 기기의 공개 예시에는 어떤 절차가 있나요?"
        config = manifest()
        config["question_sha256s"] = [hashlib.sha256(question.encode("utf-8")).hexdigest()]
        inputs = variables("answer")
        inputs["question"] = question
        protocol = PublicProtocol(config)
        prepared = protocol.prepare("answer", inputs, [SOURCE])
        reply = {"answer": "공개 예시에는 서면 확인 절차가 있습니다. [S1] 실제 계약의 적용 여부는 미확인입니다.",
                 "citations": ["S1"]}
        result = protocol.decode(prepared, json.dumps(reply, ensure_ascii=False))
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(result["answer"], reply["answer"].replace("[S1]", "[7]"))
        self.assertEqual(result["cited_evidence_ids"], [SOURCE["local_id"]])
        self.assertEqual(prepared.max_output_tokens, 128)
        self.assertEqual(MAX_OUTPUT_TOKENS, 512)

    def test_bare_parenthetical_and_translated_markers_are_not_accepted(self):
        protocol = PublicProtocol(manifest())
        prepared = protocol.prepare("answer", variables("answer"), [SOURCE])
        for text in ("An example clause S1", "An example clause (S1)",
                     "예시 조항 (S1 참고)", "An example clause [source S1]"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "MARKER_INVALID"):
                protocol.decode(prepared, json.dumps({"answer": text, "citations": ["S1"]}))

    def test_answer_instruction_hash_is_bound_to_current_public_policy(self):
        config = manifest()
        config["instruction_sha256s"]["answer"] = hashlib.sha256(
            (PUBLIC_INSTRUCTIONS["answer"] + " altered").encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ValueError, "INSTRUCTION_HASH_UNKNOWN"):
            PublicProtocol(config)

    def test_optional_conditions_preserve_local_evidence_without_granting_authority(self):
        protocol = PublicProtocol(manifest())
        prepared = protocol.prepare("answer", variables("answer"), [SOURCE])
        reply = {"answer": "Written confirmation is required. [S1]", "citations": ["S1"],
                 "classification": "TRANSFERABLE",
                 "conditions": [{"text": "Obtain written confirmation. [S1]", "citations": ["S1"]}]}
        result = protocol.decode(prepared, json.dumps(reply))
        self.assertEqual(result["classification"], "TRANSFERABLE")
        self.assertEqual(result["conditions"], [{"text": "Obtain written confirmation. [7]",
                                                "evidence_ids": [SOURCE["local_id"]]}])
        self.assertEqual(set(result["conditions"][0]), {"text", "evidence_ids"})
        validate_output("answer", result, allowed_evidence_ids=[SOURCE["local_id"]])
        del reply["classification"]
        self.assertEqual(protocol.decode(prepared, json.dumps(reply))["classification"], "NEEDS_INFORMATION")

    def test_condition_citations_are_known_unique_and_within_top_level_citations(self):
        second = {**SOURCE, "local_id": "private-second-evidence", "excerpt": "A second fictional clause."}
        config = manifest()
        config["source_sha256s"].append(public_source_sha256(second))
        inputs = variables("answer")
        inputs["evidence"].append({"evidence_id": second["local_id"], "number": 8,
                                   "text": second["excerpt"], "url": second["url"],
                                   "source_type": "official_document"})
        protocol = PublicProtocol(config)
        prepared = protocol.prepare("answer", inputs, [SOURCE, second])
        for citations in ([], ["S3"], ["S1", "S1"], [True], ["S2"]):
            with self.subTest(citations=citations), self.assertRaisesRegex(ValueError, "CONDITIONS_INVALID"):
                protocol.decode(prepared, json.dumps({"answer": "First clause only. [S1]", "citations": ["S1"],
                    "conditions": [{"text": "A supported condition.", "citations": citations}]}))

    def test_conditions_are_bounded_unique_and_have_no_server_authority_fields(self):
        protocol = PublicProtocol(manifest())
        prepared = protocol.prepare("answer", variables("answer"), [SOURCE])
        row = {"text": "Obtain written confirmation.", "citations": ["S1"]}
        invalid = [row, [{"text": "x" * 2001, "citations": ["S1"]}],
                   [row, {**row, "text": " " + row["text"] + " "}],
                   [{"text": "Condition " + str(index), "citations": ["S1"]} for index in range(21)],
                   [{"text": "Unsupported marker [8]", "citations": ["S1"]}]]
        invalid.extend([[{**row, field: True}] for field in
                        ("condition_id", "fulfilled", "party_approval", "payment_allowed")])
        for conditions in invalid:
            with self.subTest(conditions=conditions), self.assertRaisesRegex(ValueError, "CONDITIONS_INVALID"):
                protocol.decode(prepared, json.dumps({"answer": "Written confirmation. [S1]", "citations": ["S1"],
                                                      "conditions": conditions}))


if __name__ == "__main__":
    unittest.main()
