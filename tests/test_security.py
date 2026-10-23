import socket
import unittest
from email.utils import formatdate
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.security import SessionStore, SourcePolicy, SafeFetcher, validate_url


SOURCE = {"source_id": "SYNTHETIC", "url": "https://example.org/terms"}


def review(action="fetch", **changes):
    result = {"source_id": "SYNTHETIC", "action": action, "status": "ALLOWED",
              "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.org/",
              "reviewed_on": "2026-01-01", "scope": "Synthetic test document",
              "conditions": "Offline fixture", "user_scope": "Local test user",
              "processing_environment": "Mock transport"}
    result.update(changes)
    return result


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


class SessionSecurityTests(unittest.TestCase):
    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_demo_requires_local_client_and_origin(self):
        sessions = SessionStore()
        for client, origin in [("8.8.8.8", "http://localhost:8000"),
                               ("127.0.0.1", "https://example.org"),
                               ("127.0.0.1", "http://localhost.attacker.test")]:
            with self.subTest(client=client, origin=origin):
                self.assert_code("DEMO_LOGIN_DISABLED", lambda: sessions.create_demo("buyer", client, origin))
        self.assertEqual(sessions.create_demo("buyer", "::1", "http://localhost:8000").actor, "buyer")

    def test_production_has_no_demo_auth_fallback(self):
        self.assert_code("DEMO_LOGIN_DISABLED", lambda: SessionStore(demo_mode=False).create_demo(
            "admin", "127.0.0.1", "https://localhost"))

    def test_csrf_checks_token_and_all_origins(self):
        sessions = SessionStore()
        session = sessions.create_demo("buyer", "127.0.0.1", "http://localhost:8000")
        self.assertEqual(sessions.require_mutation(session.id, session.csrf_token,
            "http://localhost:8000", "http://localhost:8000").actor, "buyer")
        attacks = [("wrong", "http://localhost:8000", "http://localhost:8000"),
                   ("한글", "http://localhost:8000", "http://localhost:8000"),
                   (session.csrf_token, "http://localhost:8001", "http://localhost:8000"),
                   (session.csrf_token, "http://localhost:8000", "https://localhost:8000"),
                   (session.csrf_token, "null", "http://localhost:8000"),
                   (session.csrf_token, "http://localhost:8000/evil", "http://localhost:8000"),
                   (session.csrf_token, "\nhttp://localhost:8000", "http://localhost:8000")]
        for token, origin, request_origin in attacks:
            with self.subTest(origin=origin, request_origin=request_origin):
                self.assert_code("CSRF_INVALID", lambda: sessions.require_mutation(
                    session.id, token, origin, request_origin))

    def test_identity_is_opaque_expiring_revocable(self):
        now = [10.0]
        sessions = SessionStore(ttl_seconds=10, clock=lambda: now[0])
        session = sessions.create_demo("seller", "127.0.0.1", "http://localhost")
        self.assert_code("AUTH_REQUIRED", lambda: sessions.resolve("seller"))
        self.assert_code("AUTH_REQUIRED", lambda: sessions.resolve("x" * 43))
        sessions.revoke(session.id)
        self.assert_code("AUTH_REQUIRED", lambda: sessions.resolve(session.id))
        session = sessions.create_demo("buyer", "127.0.0.1", "http://localhost")
        now[0] = 21
        self.assert_code("AUTH_REQUIRED", lambda: sessions.resolve(session.id))

    def test_unknown_role_is_not_accepted(self):
        self.assert_code("DEMO_LOGIN_DISABLED", lambda: SessionStore().create_demo(
            "DATA_MANAGER", "127.0.0.1", "http://localhost"))


class SourceSecurityTests(unittest.TestCase):
    def policy(self, reviews=None, **kwargs):
        return SourcePolicy([SOURCE], reviews, **kwargs)

    def assert_blocked(self, callback, code=None):
        with self.assertRaises(ServiceError) as raised:
            callback()
        if code:
            self.assertEqual(raised.exception.code, code)

    def test_permissions_are_independent_and_server_owned(self):
        policy = self.policy([review("view_link")], capabilities={"manager": {"DATA_MANAGER"}})
        self.assertTrue(policy.allows("SYNTHETIC", "view_link"))
        for action in ["fetch", "store_body", "extract", "local_embed", "external_process",
                       "display_excerpt", "public_redistribute", "unknown"]:
            with self.subTest(action=action):
                self.assertFalse(policy.allows("SYNTHETIC", action))
        self.assert_blocked(lambda: policy.register_source("admin", SOURCE), "FORBIDDEN")
        self.assert_blocked(lambda: policy.set_review("buyer", review()), "FORBIDDEN")
        policy.set_review("manager", review())
        self.assertTrue(policy.allows("SYNTHETIC", "fetch"))
        returned = policy.require("SYNTHETIC", "fetch")
        returned["status"] = "BLOCKED"
        self.assertTrue(policy.allows("SYNTHETIC", "fetch"))
        policy.register_source("manager", SOURCE)
        self.assertFalse(policy.allows("SYNTHETIC", "fetch"))

    def test_version_block_overrides_general_allow(self):
        policy = self.policy([review(), review(version_id="blocked", status="BLOCKED")])
        self.assertTrue(policy.allows("SYNTHETIC", "fetch", version_id="other"))
        self.assertFalse(policy.allows("SYNTHETIC", "fetch", version_id="blocked"))

    def test_fact_only_does_not_authorize_document_collection(self):
        policy = self.policy([review(basis_type="FACT_ONLY")])
        self.assertFalse(policy.allows("SYNTHETIC", "fetch"))

    def test_network_remains_disabled_even_with_review(self):
        calls = []
        fetcher = SafeFetcher(self.policy([review()]), transport=lambda url: calls.append(url), resolver=public_dns)
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "NETWORK_DISABLED")
        self.assertEqual(calls, [])

    def test_source_review_does_not_enable_other_sources(self):
        policy = self.policy([review()], network_enabled=True)
        self.assert_blocked(lambda: SafeFetcher(policy, resolver=public_dns).fetch("UNKNOWN", SOURCE["url"]), "SOURCE_BLOCKED")

    def test_url_scheme_port_credentials_host_and_parser_tricks(self):
        policy = SourcePolicy([{**SOURCE, "approved_urls": [SOURCE["url"], "https://example.org/terms?q=test"]}])
        urls = ["http://example.org/", "https://example.org:8443/", "https://x@example.org/",
                "https://example.org.evil.test/", "https://example.org./", "https://example%2eorg/",
                "https://example.org\\@evil.test/", "https://example.org/#fragment",
                "https://example.org/\nheader", "https://127.0.0.1/", "file:///tmp/source",
                "https://example.org/unregistered", "https://example.org/terms?unregistered=1"]
        for url in urls:
            with self.subTest(url=url):
                self.assert_blocked(lambda: validate_url(url, "SYNTHETIC", policy, public_dns), "URL_BLOCKED")
        result = validate_url("https://example.org/terms?q=test", "SYNTHETIC", policy, public_dns)
        self.assertEqual((result.hostname, result.addresses, result.target),
                         ("example.org", ("8.8.8.8",), "/terms?q=test"))

    def test_every_resolved_address_must_be_public(self):
        policy = self.policy()
        for ip in ["127.0.0.1", "10.0.0.1", "172.16.0.2", "192.168.0.1", "169.254.169.254",
                   "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:8.8.8.8", "100.64.0.1",
                   "2002:7f00:1::", "64:ff9b::7f00:1", "2001:0000:4136:e378:8000:63bf:3fff:fdd2"]:
            def mixed_dns(host, port, **kwargs):
                return public_dns(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
            with self.subTest(ip=ip):
                self.assert_blocked(lambda: validate_url(SOURCE["url"], "SYNTHETIC", policy, mixed_dns), "URL_BLOCKED")

    def test_redirect_default_disabled_and_optin_rechecks(self):
        policy = self.policy([review()], network_enabled=True)
        calls = []
        def redirect(url):
            calls.append(url)
            return 302, {"location": "https://127.0.0.1/internal"}, b""
        self.assert_blocked(lambda: SafeFetcher(policy, redirect, public_dns).fetch("SYNTHETIC", SOURCE["url"]), "REDIRECT_BLOCKED")
        self.assert_blocked(lambda: SafeFetcher(policy, redirect, public_dns, max_redirects=1).fetch("SYNTHETIC", SOURCE["url"]), "URL_BLOCKED")
        self.assertEqual(len(calls), 2)

    def test_redirect_dns_rebinding_is_blocked(self):
        policy = SourcePolicy([{**SOURCE, "approved_urls": [SOURCE["url"], "https://example.org/next"]}],
                              [review()], network_enabled=True)
        resolutions = [0]
        def dns(host, port, **kwargs):
            resolutions[0] += 1
            return public_dns(host, port) if resolutions[0] == 1 else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port))]
        fetcher = SafeFetcher(policy, lambda url: (302, {"location": "/next"}, b""), dns, max_redirects=1)
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "URL_BLOCKED")
        self.assertEqual(resolutions[0], 2)

    def test_retry_rate_limit_size_and_inert_bytes(self):
        policy = self.policy([review()], network_enabled=True)
        now, waits, calls = [0.0], [], []
        def sleep(delay):
            waits.append(delay)
            now[0] += delay
        malicious = b'<script>execute_payment()</script><img src="https://evil.test/">'
        def transport(validated):
            calls.append(validated)
            return (429, {"retry-after": "3"}, b"") if len(calls) == 1 else (200, {"content-type": "text/html"}, malicious)
        fetcher = SafeFetcher(policy, transport, public_dns, sleeper=sleep, clock=lambda: now[0])
        result = fetcher.fetch("SYNTHETIC", SOURCE["url"])
        self.assertEqual(result.body, malicious)
        self.assertEqual(waits, [3])
        self.assertTrue(all(item.addresses == ("8.8.8.8",) for item in calls))
        small = SafeFetcher(policy, lambda url: (200, {"content-type": "text/html"}, b"12345"), public_dns, max_bytes=4)
        self.assert_blocked(lambda: small.fetch("SYNTHETIC", SOURCE["url"]), "RESPONSE_TOO_LARGE")

    def test_retry_is_bounded_and_long_retry_after_deferred(self):
        policy = self.policy([review()], network_enabled=True)
        calls, waits = [], []
        def failing(url):
            calls.append(url)
            return 503, {}, b""
        fetcher = SafeFetcher(policy, failing, public_dns, sleeper=waits.append)
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "FETCH_FAILED")
        self.assertEqual(len(calls), 3)
        later = SafeFetcher(policy, lambda url: (429, {"retry-after": "120"}, b""), public_dns)
        self.assert_blocked(lambda: later.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")

    def test_deferred_retry_after_blocks_later_calls_to_same_host(self):
        policy = self.policy([review()], network_enabled=True)
        now, calls = [0.0], []
        def transport(url):
            calls.append(url)
            return ((429, {"retry-after": "120"}, b"") if len(calls) == 1
                    else (200, {"content-type": "text/html"}, b"ready"))
        fetcher = SafeFetcher(policy, transport, public_dns, clock=lambda: now[0])
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")
        self.assertEqual(len(calls), 1)
        now[0] = 120.0
        self.assertEqual(fetcher.fetch("SYNTHETIC", SOURCE["url"]).body, b"ready")
        self.assertEqual(len(calls), 2)

    def test_http_date_retry_after_preserves_host_cooldown(self):
        policy = self.policy([review()], network_enabled=True)
        now, calls = [0.0], []
        def transport(url):
            calls.append(url)
            return 503, {"retry-after": formatdate(1_000_120, usegmt=True)}, b""
        fetcher = SafeFetcher(policy, transport, public_dns, clock=lambda: now[0])
        with patch("warranty.security.time.time", return_value=1_000_000):
            self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")
            self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")
        self.assertEqual(len(calls), 1)

    def test_unbounded_numeric_retry_after_stays_deferred_without_overflow(self):
        for size in (400, 5000):
            with self.subTest(header_digits=size):
                policy = self.policy([review()], network_enabled=True)
                calls = []
                def transport(url):
                    calls.append(url)
                    return 429, {"retry-after": "9" * size}, b""
                fetcher = SafeFetcher(policy, transport, public_dns, clock=lambda: 0.0)
                self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")
                self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "RETRY_LATER")
                self.assertEqual(len(calls), 1)

    def test_exhausted_retry_still_delays_next_call(self):
        policy = self.policy([review()], network_enabled=True)
        now, calls, waits = [0.0], [], []
        def sleep(delay):
            waits.append(delay)
            now[0] += delay
        def transport(url):
            calls.append(url)
            return ((429, {"retry-after": "5"}, b"") if len(calls) == 1
                    else (200, {"content-type": "text/html"}, b"ready"))
        fetcher = SafeFetcher(policy, transport, public_dns, clock=lambda: now[0], sleeper=sleep,
                              max_retries=0)
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "FETCH_FAILED")
        self.assertEqual(fetcher.fetch("SYNTHETIC", SOURCE["url"]).body, b"ready")
        self.assertEqual(waits, [5.0])

    def test_network_revoked_during_rate_wait_does_not_send(self):
        policy = self.policy([review()], network_enabled=True)
        now, calls = [0.0], []
        def sleep(delay):
            now[0] += delay
            policy.network_enabled = False
        def transport(url):
            calls.append(url)
            return 200, {"content-type": "text/html"}, b"ready"
        fetcher = SafeFetcher(policy, transport, public_dns, clock=lambda: now[0], sleeper=sleep)
        fetcher.fetch("SYNTHETIC", SOURCE["url"])
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "NETWORK_DISABLED")
        self.assertEqual(len(calls), 1)

    def test_source_replaced_and_reapproved_during_wait_cannot_send_stale_url(self):
        policy = self.policy([review()], network_enabled=True,
                             capabilities={"manager": {"DATA_MANAGER"}})
        now, calls = [0.0], []
        def sleep(delay):
            now[0] += delay
            policy.register_source("manager", {**SOURCE, "url": "https://example.org/replacement"})
            policy.set_review("manager", review())
        def transport(url):
            calls.append(url)
            return 200, {"content-type": "text/html"}, b"ready"
        fetcher = SafeFetcher(policy, transport, public_dns, clock=lambda: now[0], sleeper=sleep)
        fetcher.fetch("SYNTHETIC", SOURCE["url"])
        self.assert_blocked(lambda: fetcher.fetch("SYNTHETIC", SOURCE["url"]), "SOURCE_BLOCKED")
        self.assertEqual(len(calls), 1)

    def test_transport_uses_pinned_address_and_hostname(self):
        policy = self.policy([review()], network_enabled=True)
        with patch("warranty.security._PinnedHTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheaders.return_value = [("Content-Type", "application/pdf")]
            response.read1.side_effect = [b"%PDF-test", b""]
            result = SafeFetcher(policy, resolver=public_dns).fetch("SYNTHETIC", SOURCE["url"])
        connection.assert_called_once_with("example.org", "8.8.8.8", 5)
        self.assertEqual(result.body, b"%PDF-test")

    def test_source_limits_take_precedence(self):
        policy = SourcePolicy([{**SOURCE, "http_limits": {"min_interval_seconds": 4,
            "max_retries": 0, "max_bytes": 4, "connect_timeout": 1, "read_timeout": 2,
            "max_redirects": 0, "user_agent": "SourceApprovedAgent/1"}}], [review()], network_enabled=True)
        fetcher = SafeFetcher(policy, resolver=public_dns)
        with patch("warranty.security._PinnedHTTPSConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheaders.return_value = [("Content-Type", "application/pdf")]
            response.read1.side_effect = [b"test", b""]
            fetcher.fetch("SYNTHETIC", SOURCE["url"])
            connection.assert_called_once_with("example.org", "8.8.8.8", 1)
            self.assertEqual(connection.return_value.request.call_args.kwargs["headers"]["User-Agent"],
                             "SourceApprovedAgent/1")
        calls = []
        def fail(url):
            calls.append(url)
            return 503, {}, b""
        limited = SafeFetcher(policy, fail, public_dns)
        self.assert_blocked(lambda: limited.fetch("SYNTHETIC", SOURCE["url"]), "FETCH_FAILED")
        self.assertEqual(len(calls), 1)
        oversized = SafeFetcher(policy, lambda url: (200, {"content-type": "text/html"}, b"12345"), public_dns)
        self.assert_blocked(lambda: oversized.fetch("SYNTHETIC", SOURCE["url"]), "RESPONSE_TOO_LARGE")
        with self.assertRaises(ValueError):
            SourcePolicy([{**SOURCE, "http_limits": {"unknown_constraint": 1}}])


if __name__ == "__main__":
    unittest.main()
