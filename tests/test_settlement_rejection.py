"""A payment the bank accepted, then rejected on its settlement day.

    python3 -m unittest -v tests.test_settlement_rejection

mock-bank 0.9.0 rejects an accepted payment when the debtor's account closes
before the payment settles: the camt.054 it promised for that day is
withdrawn, and a second pain.002 rejects the payment AC04 in its place. An
ACH account is sent a NACHA return file instead, with R02. Nothing books, so
no statement ever shows it. (mock-films' bank_closed_film
draws the same run.)

Each test asserts what should happen, and checks first that the bank really
sent the rejection, so a failure here is the example's and not the bank's.
"""
import datetime
import json
import unittest

from mockacme.bank_messages import call
from tests import test_pay_invoices, test_payment_run
from mockacme.payment_run import PaymentRun
from tests.test_payment_run import BANK, CLOSED, GLOBEX, SAP, StatementCase, control


def rejections_at_settlement():
    """How many payments ACME's mailbox rejects, and the messages that do.

    A pain.002's rejected transactions, or a NACHA return file's addenda
    with the closed account's R02.
    """
    status, raw = call(BANK, "GET", "/_mock/mailbox?leave")
    messages = [m for m in json.loads(raw) if m["account"] == "ACME"]
    count = sum(m["body"].count("<TxSts>RJCT</TxSts>") for m in messages
                if m["type"].startswith("pain.002"))
    count += sum(line.startswith("799R02") for m in messages
                 if m["type"] == "nacha.return" for line in m["body"].splitlines())
    return count, "\n".join(m["key"] for m in messages)


class PayInvoicesSettlementRejection(unittest.TestCase):
    # pay_invoices' own case set up the mocks and has the helpers; they are
    # borrowed rather than inherited, so its tests do not run twice.
    _case = test_pay_invoices.PayingSupplierInvoices
    setUp = _case.setUp
    invoiced, bank_behaves, settle, statuses = (
        _case.invoiced, _case.bank_behaves, _case.settle, _case.statuses)

    def test_a_payment_rejected_at_settlement_reopens_the_invoice(self):
        [key] = self.invoiced("PO-7101")["new"]
        self.run_.pay()
        self.run_.reconcile()
        self.assertEqual(self.statuses()[key], ("scheduled", ""))

        # Between acceptance and the due date, ACME's own account closes.
        self.bank_behaves(closed=True)
        self.settle()
        count, text = rejections_at_settlement()
        self.assertEqual(count, 1, "mock-bank sent no rejection at settlement:\n" + text)

        self.run_.reconcile()
        status, problem = self.statuses()[key]
        # Rejected like a payment refused on acceptance: open, and held.
        self.assertEqual(status, "open", "still %r after the bank rejected it" % status)
        self.assertTrue(problem.startswith("AC04"), problem)


class PaymentRunSettlementRejection(StatementCase):

    def test_a_payment_rejected_at_settlement_is_let_go(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.payments.run(self.today, "R1")      # Friday 16:00: settles Monday
        [item] = run.items
        self.assertEqual(item.status, "accepted")
        self.assertEqual(self.claim("GLX-4711")[0], "R1")

        control(BANK, "PATCH", "/_mock/accounts/ACME", {"closed": True})
        self.advance(self.monday + datetime.timedelta(days=1))
        count, text = rejections_at_settlement()
        self.assertEqual(count, 1, "mock-bank sent no rejection at settlement:\n" + text)

        self.payments.reconcile(run)
        self.assertEqual((item.status, item.reason[:4]), ("rejected", CLOSED),
                         "the run still has it as %r" % item.status)
        # Rejected moved no money: the claim comes off, as for a refusal on
        # acceptance, so the invoice is free for the next run.
        self.assertEqual(self.claim("GLX-4711"), ("", ""))

    def test_the_next_run_does_not_skip_it_forever(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.payments.run(self.today, "R1")
        control(BANK, "PATCH", "/_mock/accounts/ACME", {"closed": True})
        self.advance(self.monday + datetime.timedelta(days=1))
        count, _ = rejections_at_settlement()
        self.assertEqual(count, 1)
        control(BANK, "PATCH", "/_mock/accounts/ACME", {"closed": False})

        [again] = self.another().run(self.monday + datetime.timedelta(days=1), "R2").items
        self.assertNotEqual(again.status, "skipped", again.reason)

    # Borrowed from the payment-run case they were written for.
    claim = test_payment_run.ASecondRunBeforeTheStatement.claim
    another = test_payment_run.ASecondRunBeforeTheStatement.another


# -- what the fix reads, and what it leaves alone ----------------------------

SETTLEMENT_REPORT = (
    "<?xml version='1.0' encoding='utf-8'?>"
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.002.001.10"><CstmrPmtStsRpt>'
    "<GrpHdr><MsgId>MB-P002-S000001</MsgId></GrpHdr>"
    "<OrgnlGrpInfAndSts><OrgnlMsgId>F110-20261002-R1</OrgnlMsgId>"
    "<OrgnlMsgNmId>pain.001.001.09</OrgnlMsgNmId></OrgnlGrpInfAndSts>"
    "<OrgnlPmtInfAndSts><OrgnlPmtInfId>F110-20261002-R1-1</OrgnlPmtInfId>"
    "<TxInfAndSts><OrgnlEndToEndId>GLX-4711</OrgnlEndToEndId><TxSts>RJCT</TxSts>"
    "<StsRsnInf><Rsn><Cd>AC04</Cd></Rsn><AddtlInf>the debtor account was closed before "
    "the payment settled</AddtlInf></StsRsnInf></TxInfAndSts>"
    "<TxInfAndSts><OrgnlEndToEndId>UMB-0815</OrgnlEndToEndId><TxSts>ACSC</TxSts>"
    "</TxInfAndSts></OrgnlPmtInfAndSts></CstmrPmtStsRpt></Document>")

# As mock-bank 0.9.0 writes one for a payment refused when it was to settle.
RETURN_FILE = "\n".join((
    "101 0000000001 9999999922610060000A094101ACME Corporation       MOCK BANK          "
    "            ",
    "5220ACME Corporation                    0000000001CCDRETURN          261005   1999999"
    "990000001",
    "6219999999920000000002       0000119000GLX-4711       GLOBEX                  199999"
    "9990000001",
    "799R02999999990000001      99999999                                            99999"
    "9990000001",
    "822000000200999999990000000000000000001190000000000001                         99999"
    "9990000001"))


def item(**changes):
    from mockacme.payment_run import Item
    return Item(**dict(dict(document="1710/2026/5100000001", supplier=GLOBEX,
                            reference="GLX-4711", amount="1190.00", routing="999999992",
                            account="0000000002", run_id="R1", run_date="2026-10-02"),
                       **changes))


class ReadingARefusalAfterAcceptance(unittest.TestCase):
    def test_a_status_report_gives_its_rejections_and_nothing_else(self):
        from mockacme.payment_run import refused_transactions
        self.assertEqual(refused_transactions(SETTLEMENT_REPORT), {
            ("F110-20261002-R1", "GLX-4711"):
                ("AC04", "the debtor account was closed before the payment settled")})
        self.assertEqual(refused_transactions(""), {})

    def test_a_return_file_gives_each_entry_with_its_account_and_reason(self):
        from mockacme.payment_run import nacha_returned_entries
        from decimal import Decimal
        self.assertEqual(nacha_returned_entries(RETURN_FILE), [
            {"account": "0000000002", "amount": Decimal("1190.00"), "reference": "GLX-4711",
             "code": "R02", "routing": "99999999"}])
        self.assertEqual(nacha_returned_entries("799R02 an addenda with no entry"), [])

    def test_a_report_is_about_the_run_whose_file_it_names(self):
        from mockacme.payment_run import PaymentRun, refused_transactions
        payments = PaymentRun("http://sap", "http://bank", control_free_company(), "iso20022")
        late = {"files": refused_transactions(SETTLEMENT_REPORT), "entries": [], "unread": ""}
        self.assertEqual(payments.refused_later(item(), late)[0], "AC04")
        for other in (item(run_id="R2"), item(run_date="2026-10-05"), item(run_date=""),
                      item(run_date="not a date"), item(reference="UMB-0815")):
            self.assertEqual(payments.refused_later(other, late), ("", ""), other)

    def test_a_return_entry_is_about_a_payment_of_that_much_to_that_account(self):
        from mockacme.payment_run import PaymentRun, nacha_returned_entries
        payments = PaymentRun("http://sap", "http://bank", control_free_company(), "nacha")
        late = {"files": {}, "entries": nacha_returned_entries(RETURN_FILE), "unread": ""}
        self.assertEqual(payments.refused_later(item(), late)[0], "R02")
        for other in (item(amount="1190.01"), item(account="0000000003"),
                      item(routing="999999982"), item(reference="GLX-4712")):
            self.assertEqual(payments.refused_later(other, late), ("", ""), other)


def control_free_company():
    return dict(test_payment_run.ACME)


class PayInvoicesReadingIt(unittest.TestCase):
    """pay_invoices with no mock at all: a ledger and a report."""

    def ledger(self, status):
        from mockacme.pay_invoices import PayInvoices
        run = PayInvoices("http://edi", "http://bank", {"id": "ACME", "name": "ACME",
                                                        "iban": "X", "bic": "Y"}, {})
        run.ledger["MOCKEDI/GLX-4711"] = {
            "invoice": {"number": "GLX-4711", "total": "1190.00"}, "status": status,
            "problem": "", "msg_id": "F110-20261002-R1", "settles_on": "", "booked_on": ""}
        from mockacme.bank_messages import bank_documents
        [root] = bank_documents(SETTLEMENT_REPORT)
        run.read_status_report(root[0])
        return run.ledger["MOCKEDI/GLX-4711"]

    def test_scheduled_or_notified_goes_back_to_open_and_held(self):
        for status in ("scheduled", "notified"):
            record = self.ledger(status)
            self.assertEqual(record["status"], "open", status)
            self.assertTrue(record["problem"].startswith(
                "AC04: the debtor account was closed before the payment settled (refused "
                "when it was to settle"), record["problem"])

    def test_paid_stays_paid_and_open_stays_as_it_was(self):
        for status in ("paid", "open"):
            self.assertEqual((self.ledger(status)["status"], self.ledger(status)["problem"]),
                             (status, ""), status)


class AfterTheRefusalIsRead(StatementCase):

    def refused(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.payments.run(self.today, "R1")
        control(BANK, "PATCH", "/_mock/accounts/ACME", {"closed": True})
        self.advance(self.monday + datetime.timedelta(days=1))
        return run

    def test_the_run_says_when_it_was_refused_and_a_second_reading_changes_nothing(self):
        run = self.refused()
        self.payments.reconcile(run)
        [problem] = [p for p in run.problems if "GLX-4711" in p]
        self.assertTrue(problem.startswith(
            "the bank refused GLX-4711 when it was to settle, after it had accepted it: "
            + CLOSED), problem)
        self.payments.reconcile(run)
        self.assertEqual(([i.status for i in run.items],
                          len([p for p in run.problems if "GLX-4711" in p])),
                         (["rejected"], 1))

    def test_the_next_run_says_whose_claim_it_took_off(self):
        self.refused()
        control(BANK, "PATCH", "/_mock/accounts/ACME", {"closed": False})
        second = self.another().run(self.monday + datetime.timedelta(days=1), "R2")
        [problem] = [p for p in second.problems if "GLX-4711" in p]
        self.assertIn("from payment run R1 of 2026-10-02 after it had accepted it (%s"
                      % CLOSED, problem)
        self.assertEqual(self.claim("GLX-4711")[0], "R2")

    def test_a_bank_that_cannot_say_is_a_problem_where_another_run_holds_an_item(self):
        self.post_two()
        self.payments.run(self.today, "R1")
        nowhere = PaymentRun(SAP, SAP, test_payment_run.ACME, test_payment_run.MODE)
        second = nowhere.run(self.monday, "R2")
        self.assertEqual([i.status for i in second.items], ["skipped", "skipped"])
        self.assertIn("so whether the bank refused any payment another run holds was "
                      "not read", second.problems[0])
        self.assertEqual(len([p for p in second.problems if "another run holds" in p]), 1)

    claim = test_payment_run.ASecondRunBeforeTheStatement.claim
    another = test_payment_run.ASecondRunBeforeTheStatement.another


if __name__ == "__main__":
    unittest.main()
