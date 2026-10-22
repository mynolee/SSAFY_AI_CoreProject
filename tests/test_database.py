"""Synthetic SQLite integrity, posting, rollback, and contention checks."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

import warranty.db as database_module
from warranty.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="warranty-db-test-")
        self.db = Database(Path(self.directory.name) / "private.sqlite3")
        self.db.initialize()
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO items(item_id,name,provider) VALUES('SYNTHETIC_ITEM','Test','SYNTHETIC')")

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    @staticmethod
    def trade(connection, trade_id="t1", D=100, W=0):
        connection.execute(
            "INSERT INTO trades(trade_id,buyer_id,seller_id,item_id) VALUES(?,'buyer','seller','SYNTHETIC_ITEM')",
            (trade_id,),
        )
        connection.execute(
            "INSERT INTO contract_revisions(trade_id,revision,hash,item_id,D,W,terms_json) VALUES(?,1,'server-hash','SYNTHETIC_ITEM',?,?,'{}')",
            (trade_id, D, W),
        )
        for component in ("D", "W"):
            connection.execute("INSERT INTO component_balances(trade_id,component) VALUES(?,?)", (trade_id, component))

    @staticmethod
    def group(connection, group_id="l1", trade_id="t1", operation_id=None,
              attempt=1, event="DEPOSIT", amount=100):
        connection.execute(
            "INSERT INTO ledger_groups(group_id,trade_id,operation_id,attempt_no,event_type) VALUES(?,?,?,?,?)",
            (group_id, trade_id, operation_id, attempt, event),
        )
        connection.executemany(
            "INSERT INTO ledger_entries(group_id,line_no,account,debit,credit) VALUES(?,?,?,?,?)",
            [(group_id, 1, "buyer:wallet", amount, 0),
             (group_id, 2, "trade:held", 0, amount)],
        )
        connection.execute("UPDATE ledger_groups SET posted=1 WHERE group_id=?", (group_id,))

    @staticmethod
    def operation(connection, trade_id="t1", proposal_id="p1", group_id="g1", operation_id="o1"):
        connection.execute(
            "INSERT INTO settlement_proposals(proposal_id,trade_id,revision,hash,contract_revision,payload_json) VALUES(?,?,1,'proposal-hash',1,'{}')",
            (proposal_id, trade_id),
        )
        connection.execute("INSERT INTO allocations(proposal_id,component,pay_won,refund_won) VALUES(?,'D',100,0)", (proposal_id,))
        connection.execute("INSERT INTO settlement_groups(group_id,trade_id,proposal_id,revision,status) VALUES(?,?,?,1,'RESERVED')", (group_id, trade_id, proposal_id))
        connection.execute(
            "INSERT INTO settlement_operations(operation_id,trade_id,group_id,proposal_id,revision,component,direction,amount,status) VALUES(?,?,?,?,1,'D','PAY',100,'RESERVED')",
            (operation_id, trade_id, group_id, proposal_id),
        )

    def test_connection_settings_migration_reentry_and_seed_preservation(self):
        with self.db.transaction() as connection:
            connection.execute("UPDATE demo_wallets SET available=123 WHERE user_id='buyer'")
        self.db.initialize()
        connection = self.db.connect()
        try:
            self.assertIs(connection.row_factory, sqlite3.Row)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 2000)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0], 123)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

    def test_connection_context_releases_the_sqlite_handle(self):
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_applied_old_schema_upgrades_without_reseeding_or_losing_data(self):
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        migration_dir = Path(database_module.__file__).with_name("migrations")
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT 'SYNTHETIC')")
            for version, name in ((1, "001_initial.sql"), (2, "002_reference_guards.sql")):
                raw = (migration_dir / name).read_bytes()
                connection.executescript(raw.decode("utf-8"))
                connection.execute("INSERT INTO schema_migrations(version,name,sha256) VALUES(?,?,?)", (version, name, hashlib.sha256(raw).hexdigest()))
            connection.execute("UPDATE demo_wallets SET available=987 WHERE user_id='buyer'")
            connection.commit()
        finally:
            connection.close()
        upgraded = Database(legacy_path)
        try:
            upgraded.initialize()
            upgraded.initialize()
            with upgraded.connect() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertEqual(connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0], 987)
                self.assertIn("review_id", [r["name"] for r in connection.execute("PRAGMA table_info(contract_revisions)")])
                self.assertIn("user_scope", [r["name"] for r in connection.execute("PRAGMA table_info(source_action_reviews)")])
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            upgraded.close()

    def test_writer_contention_has_a_finite_timeout(self):
        bounded = Database(self.db.path, busy_timeout_ms=25, begin_attempts=2)
        owner = self.db.connect()
        owner.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with bounded.transaction():
                    self.fail("A second writer acquired a held writer lock")
            self.assertLess(time.monotonic() - started, 2)
        finally:
            owner.rollback()
            owner.close()
            bounded.close()

    def test_trade_and_contract_are_one_deferred_atomic_creation(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO trades(trade_id,buyer_id,seller_id,item_id) VALUES('orphan','buyer','seller','SYNTHETIC_ITEM')")
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
            self.trade(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("UPDATE contract_revisions SET D=1 WHERE trade_id='t1'")

    def test_nonnegative_conservation_and_exact_integer_checks(self):
        with self.db.transaction() as connection:
            self.trade(connection)
        for statement in (
            "UPDATE component_balances SET held=1 WHERE trade_id='t1' AND component='D'",
            "UPDATE component_balances SET deposited=-1,held=-1 WHERE trade_id='t1' AND component='D'",
            "UPDATE demo_wallets SET available=-1 WHERE user_id='buyer'",
            "UPDATE demo_wallets SET available=1.5 WHERE user_id='buyer'",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.transaction() as connection:
                        connection.execute(statement)

    def test_cross_trade_child_reference_is_rejected(self):
        with self.db.transaction() as connection:
            self.trade(connection, "t1")
            self.trade(connection, "t2")
            connection.execute("INSERT INTO evidence_bundles(evidence_id,trade_id,revision,hash,contract_revision,payload_json) VALUES('e1','t1',1,'hash',1,'{}')")
            self.operation(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("UPDATE trades SET evidence_id='e1' WHERE trade_id='t2'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO settlement_operations(operation_id,trade_id,group_id,proposal_id,revision,component,direction,amount,status) VALUES('o2','t2','g1','p1',1,'D','REFUND',1,'RESERVED')")

    def test_unbalanced_and_unposted_ledgers_cannot_commit(self):
        with self.db.transaction() as connection:
            self.trade(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO ledger_groups(group_id,trade_id,event_type) VALUES('bad','t1','DEPOSIT')")
                connection.execute("INSERT INTO ledger_entries(group_id,line_no,account,debit,credit) VALUES('bad',1,'wallet',100,0)")
                connection.execute("UPDATE ledger_groups SET posted=1 WHERE group_id='bad'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO ledger_groups(group_id,trade_id,event_type) VALUES('open','t1','DEPOSIT')")
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger_groups").fetchone()[0], 0)

    def test_posted_ledger_entries_and_group_are_append_only(self):
        with self.db.transaction() as connection:
            self.trade(connection)
            self.group(connection)
        for statement in (
            "UPDATE ledger_entries SET debit=99 WHERE group_id='l1' AND line_no=1",
            "DELETE FROM ledger_entries WHERE group_id='l1'",
            "UPDATE ledger_groups SET event_type='OTHER' WHERE group_id='l1'",
            "DELETE FROM ledger_groups WHERE group_id='l1'",
            "INSERT INTO ledger_entries(group_id,line_no,account,debit,credit) VALUES('l1',3,'extra',1,0)",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.transaction() as connection:
                        connection.execute(statement)

    def test_logical_operation_and_attempt_event_uniqueness(self):
        with self.db.transaction() as connection:
            self.trade(connection)
            self.operation(connection)
            self.group(connection, "reserve1", operation_id="o1", event="RESERVE")
            self.group(connection, "release1", operation_id="o1", event="RELEASE")
            connection.execute("UPDATE settlement_operations SET status='FAILED' WHERE operation_id='o1'")
            connection.execute("UPDATE settlement_operations SET status='RESERVED',attempt_no=2 WHERE operation_id='o1'")
            self.group(connection, "reserve2", operation_id="o1", attempt=2, event="RESERVE")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO ledger_groups(group_id,trade_id,operation_id,attempt_no,event_type) VALUES('duplicate','t1','o1',2,'RESERVE')")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO settlement_operations(operation_id,trade_id,group_id,proposal_id,revision,component,direction,amount,status) VALUES('duplicate','t1','g1','p1',1,'D','PAY',100,'RESERVED')")
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger_groups").fetchone()[0], 3)

    def test_exception_rolls_back_wallet_balances_and_ledger_together(self):
        with self.db.transaction() as connection:
            self.trade(connection)
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            with self.db.transaction() as connection:
                connection.execute("UPDATE demo_wallets SET available=available-100 WHERE user_id='buyer'")
                connection.execute("UPDATE component_balances SET deposited=100,held=100 WHERE trade_id='t1' AND component='D'")
                self.group(connection)
                raise RuntimeError("simulated crash")
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0], 2000000)
            self.assertEqual(connection.execute("SELECT deposited FROM component_balances WHERE trade_id='t1' AND component='D'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger_groups").fetchone()[0], 0)

    def test_consent_hash_party_and_cross_trade_target_guards(self):
        with self.db.transaction() as connection:
            self.trade(connection)
            self.trade(connection, "t2")
            self.operation(connection)
        invalid = [
            ("t1", "contract", "t1", 1, "admin", "server-hash"),
            ("t1", "contract", "t1", 1, "buyer", "client-invented-hash"),
            ("t2", "proposal", "p1", 1, "buyer", "proposal-hash"),
        ]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.transaction() as connection:
                        connection.execute("INSERT INTO consents(trade_id,target_type,target_id,revision,actor,target_hash) VALUES(?,?,?,?,?,?)", values)
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO consents(trade_id,target_type,target_id,revision,actor,target_hash) VALUES('t1','contract','t1',1,'buyer','server-hash')")

    def test_future_attempt_and_mutated_logical_operation_are_rejected(self):
        with self.db.transaction() as connection:
            self.trade(connection)
            self.operation(connection)
        for statement in (
            "INSERT INTO ledger_groups(group_id,trade_id,operation_id,attempt_no,event_type) VALUES('future','t1','o1',2,'RESERVE')",
            "UPDATE settlement_operations SET attempt_no=2 WHERE operation_id='o1'",
            "UPDATE settlement_operations SET amount=1 WHERE operation_id='o1'",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.transaction() as connection:
                        connection.execute(statement)

    def test_terminal_operations_and_trusted_event_history_cannot_be_rewritten(self):
        with self.db.transaction() as connection:
            self.trade(connection)
            self.operation(connection)
            connection.execute("UPDATE settlement_operations SET status='SUCCEEDED' WHERE operation_id='o1'")
            connection.execute("INSERT INTO simulation_events(event_id,operation_id,attempt_no,outcome) VALUES('event1','o1',1,'SUCCEEDED')")
        for statement in (
            "UPDATE settlement_operations SET status='FAILED' WHERE operation_id='o1'",
            "UPDATE simulation_events SET outcome='FAILED' WHERE event_id='event1'",
            "DELETE FROM simulation_events WHERE event_id='event1'",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.transaction() as connection:
                        connection.execute(statement)
        with self.db.transaction() as connection:
            connection.execute("UPDATE simulation_events SET applied=1 WHERE event_id='event1'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("UPDATE simulation_events SET applied=0 WHERE event_id='event1'")

    def test_two_trade_deposits_serialize_wallet_and_only_one_fits(self):
        with self.db.transaction() as connection:
            self.trade(connection, "t1", D=80)
            self.trade(connection, "t2", D=80)
            connection.execute("UPDATE demo_wallets SET available=100 WHERE user_id='buyer'")
        barrier = threading.Barrier(2)

        def deposit(trade_id):
            barrier.wait(timeout=5)
            with self.db.transaction() as connection:
                changed = connection.execute("UPDATE demo_wallets SET available=available-80 WHERE user_id='buyer' AND available>=80").rowcount
                if not changed:
                    return False
                connection.execute("UPDATE component_balances SET deposited=80,held=80 WHERE trade_id=? AND component='D'", (trade_id,))
                self.group(connection, "ledger-" + trade_id, trade_id=trade_id, amount=80)
                return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(deposit, ("t1", "t2")))
        self.assertEqual(sorted(results), [False, True])
        with self.db.transaction() as connection:
            self.assertEqual(connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0], 20)
            self.assertEqual(connection.execute("SELECT SUM(deposited) FROM component_balances").fetchone()[0], 80)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger_groups").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
