"""Retry failed collection outcomes without discarding successful bodies."""
import unittest

import test_collection as fixtures


class CollectionRetryTests(unittest.TestCase):
    setUp = fixtures.CollectionTests.setUp
    target = fixtures.CollectionTests.target
    collect = fixtures.CollectionTests.collect

    def test_retry_failed_bypasses_failure_ttl_and_preserves_success_on_next_retry(self):
        self.target()
        self.fetcher.failures = 1
        self.assertEqual(self.collect()["status"], "FAILED")
        self.assertEqual(self.collect()["status"], "FAILED")
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(self.collect(retry_failed=True)["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 2)
        self.assertEqual(self.collect(retry_failed=True)["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_retry_failed_bypasses_no_results_without_waiting_for_ttl(self):
        self.assertEqual(self.collect()["status"], "NO_RESULTS")
        self.assertEqual(self.collect()["cache_hits"], 1)
        self.assertEqual(len(self.search.calls), 1)
        retried = self.collect(retry_failed=True)
        self.assertEqual(retried["status"], "NO_RESULTS")
        self.assertEqual(retried["cache_hits"], 0)
        self.assertEqual(len(self.search.calls), 2)
        self.assertEqual(self.fetcher.calls, [])


if __name__ == "__main__":
    unittest.main()
