import tempfile
import unittest
from pathlib import Path
from scripts.audit_budget import AuditBudget

class AuditBudgetTests(unittest.TestCase):
    def test_in_flight_reservations_cannot_exceed_the_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            budget=AuditBudget(Path(directory)/'budget.json',limit=5)
            first=budget.reserve(100000,16384)
            second=budget.reserve(100000,16384)
            third=budget.reserve(100000,16384)
            with self.assertRaisesRegex(RuntimeError,'RMB'):
                budget.reserve(100000,16384)
            self.assertLessEqual(budget.total,5)
            budget.settle(first,1000,1000)
            budget.reserve(100000,16384)

    def test_unknown_usage_keeps_reservation_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'budget.json'
            budget=AuditBudget(path,limit=5)
            ticket=budget.reserve(10000,16384)
            budget.settle(ticket,None,None)
            self.assertEqual(AuditBudget(path,limit=5).total,budget.total)
            self.assertGreater(budget.total,.4)

    def test_settlement_uses_peak_cache_miss_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            budget=AuditBudget(Path(directory)/'budget.json',limit=5)
            ticket=budget.reserve(10000,16384)
            budget.settle(ticket,1000,1000)
            self.assertAlmostEqual(budget.total,.036)

    def test_offpeak_rates_reserve_and_settle_within_the_same_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            budget=AuditBudget(Path(directory)/'budget.json',limit=5,rates=(4.5,13.5))
            ticket=budget.reserve(100000,16384)
            self.assertAlmostEqual(budget.total,.671184)
            budget.settle(ticket,1000,1000)
            self.assertAlmostEqual(budget.total,.018)

    def test_in_flight_refund_allows_a_waiting_request_without_exceeding_limit(self):
        import threading
        with tempfile.TemporaryDirectory() as directory:
            budget=AuditBudget(Path(directory)/'budget.json',limit=1,wait_seconds=2)
            first=budget.reserve(50000,10000)
            started=threading.Event();completed=threading.Event();errors=[]
            def request():
                started.set()
                try: budget.reserve(50000,10000)
                except Exception as exc: errors.append(exc)
                finally: completed.set()
            thread=threading.Thread(target=request);thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(completed.wait(.05))
            budget.settle(first,1000,1000)
            thread.join(2)
            self.assertTrue(completed.is_set())
            self.assertEqual(errors,[])
            self.assertLessEqual(budget.total,1)
