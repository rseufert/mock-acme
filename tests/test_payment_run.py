"""Integration tests for payment_run, against mock-sap and mock-bank.

    python3 -m unittest -v tests.test_payment_run

`tests/__init__.py` starts the mocks, and says how to point the tests at
mocks that are already running.

mock-bank starts at 16:00 on a Friday, after its 15:00 cutoff, and a reset
returns it there. Test 6 needs exactly that moment, and the others advance to
Monday morning first, so each test knows which statement its payments are on.

mock-sap's seed has suppliers banking at the accounts mock-bank holds, but no
supplier invoices, so each test posts its own as inbound `INVOIC` IDocs, the
chain a real system runs. A block is set the way a real system sets one, on
the supplier invoice, and SAP carries it to the open item. Nothing here writes
to the open-item cube, which is read-only in SAP and in mock-sap.
"""
import datetime
import json
import os
import re
import socket
import tempfile
import unittest
import unittest.mock
import urllib.parse
import urllib.request
from decimal import Decimal

from mockacme import payment_run as payment_run_module
from mockacme.bank_messages import call
from mockacme.payment_run import (ITEMS, ODATA, OPEN_SUPPLIER_ITEMS, Item, PaymentRun,
                                  Register, Run, SapSession, nacha_time_and_modifier,
                                  odata, sap_date)

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
BANK = os.environ.get("BANK_URL", "http://127.0.0.1:8090")

# The same tests in either mode: ISO 20022 by default, or NACHA with
# PAYMENT_RUN_FORMAT=nacha (mock-bank#55), which is the 0.3 milestone's definition of
# done. In NACHA mode ACME is a dollar account the bank knows by company
# identification, and the suppliers are paid by routing and account number.
MODE = os.environ.get("PAYMENT_RUN_FORMAT", "iso20022")
if MODE not in ("iso20022", "nacha"):
    # A misspelt mode would otherwise run every test in ISO mode and pass.
    raise ValueError("PAYMENT_RUN_FORMAT is iso20022 or nacha, not %r" % MODE)
NACHA = MODE == "nacha"
CURRENCY = "USD" if NACHA else "EUR"
CLOSED = "R02" if NACHA else "AC04"        # how the closed account is answered

ACME = {"name": "ACME Corporation", "iban": "NL41MOCK0000000001", "bic": "MOCKNL2A",
        "company_id": "0000000001", "routing": "999999992"}

# mock-sap's seeded suppliers, which bank where mock-bank's seed says they do
# (mock-sap 0.13.1). INITECH's account is closed at the bank, and SAP still
# believes in it: stale bank details.
GLOBEX, INITECH, UMBRELLA = "1000013", "1000014", "1000016"
ACCOUNTS = {
    GLOBEX: "NL14MOCK0000000002",       # held by mock-bank, open
    UMBRELLA: "NL30MOCK0000000005",     # at another bank: settles
    INITECH: "NL84MOCK0000000003",      # held by mock-bank, closed: AC04
}
# The same three in NACHA mode: routing and account number. GLOBEX and INITECH
# at mock-bank's routing number with their account numbers there; Umbrella at
# another bank.
DOMESTIC = {GLOBEX: ("999999992", "0000000002"),
            INITECH: ("999999992", "0000000003"),
            UMBRELLA: ("021000021", "0000000005")}


def closed_port():
    """A URL on this machine that nothing listens on: bound, then let go."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return "http://127.0.0.1:%d" % probe.getsockname()[1]


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class Sap(SapSession):
    """The test's own writes to mock-sap: vendor master and inbound invoices."""

    def __init__(self, today):
        super().__init__(SAP)
        self.today = today

    def invoice(self, supplier, reference, gross, terms="0001", dated=None):
        """An inbound INVOIC: the supplier bills us, and SAP posts a payable.

        Returns what SAP posted: the supplier invoice, its fiscal year and the
        accounting document behind the open item.
        """
        # The bank's date, not the host's: the run compares the due date with
        # the bank's today, and the two differ whenever bank time is pinned.
        dated = (dated or self.today).strftime("%Y%m%d")
        receipt = self.write("POST", "/sap/bc/idoc/idoc_xml", """<?xml version="1.0"?>
<INVOIC02><IDOC BEGIN="1">
<EDI_DC40 SEGMENT="1"><IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP></EDI_DC40>
<E1EDK01 SEGMENT="1"><CURCY>%s</CURCY><ZTERM>%s</ZTERM></E1EDK01>
<E1EDKA1 SEGMENT="1"><PARVW>LF</PARVW><PARTN>%s</PARTN></E1EDKA1>
<E1EDK02 SEGMENT="1"><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>
<E1EDK03 SEGMENT="1"><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>
<E1EDS01 SEGMENT="1"><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>
</IDOC></INVOIC02>""" % (CURRENCY, terms, supplier, reference, dated, gross),
            "application/xml")
        return json.loads(receipt)["APPLIED"][0]

    def domestic_bank(self, supplier, routing, account):
        """Give a supplier's account the US details a NACHA run pays to: the
        vendor master, kept the way a real one is, through its API."""
        self.write("PATCH", ODATA + "/API_BUSINESS_PARTNER_SRV/A_BusinessPartnerBank"
                   "(BusinessPartner='%s',BankIdentification='0001')" % supplier,
                   json.dumps({"BankNumber": routing, "BankAccount": account}),
                   "application/json")

    def holder(self, supplier, name):
        """Name the holder of a supplier's account, which is who a payment is to."""
        self.write("PATCH", ODATA + "/API_BUSINESS_PARTNER_SRV/A_BusinessPartnerBank"
                   "(BusinessPartner='%s',BankIdentification='0001')" % supplier,
                   json.dumps({"BankAccountHolderName": name}), "application/json")

    def block(self, posted, reason="A"):
        """Block a posted invoice for payment, on the invoice, as SAP users do."""
        self.write("PATCH", ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
                   "(SupplierInvoice='%s',FiscalYear='%s')"
                   % (posted["SUPPLIERINVOICE"], posted["FISCALYEAR"]),
                   json.dumps({"PaymentBlockingReason": reason}), "application/json")


class MocksCase(unittest.TestCase):
    """Both mocks reset, and bank time read from the bank."""

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(BANK, "POST", "/_mock/reset")
        self.today = self.bank_today()
        self.sap = Sap(self.today)
        if NACHA:
            control(BANK, "PATCH", "/_mock/accounts/ACME",
                    {"format": "nacha", "currency": "USD"})
            for supplier, (routing, account) in DOMESTIC.items():
                self.sap.domestic_bank(supplier, routing, account)
        self.payments = PaymentRun(SAP, BANK, ACME, MODE)

    def bank_today(self):
        return datetime.date.fromisoformat(
            control(BANK, "GET", "/_mock/state")["clock"]["date"])

    def post_three(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
        self.sap.invoice(INITECH, "INI-2026-17", "595.00")

    def by_reference(self, run):
        return {item.reference: item for item in run.items}


class PayingOpenItems(MocksCase):

    def test_a_closed_account_is_rejected_ac04_and_the_rest_accepted(self):
        self.post_three()
        run = self.payments.run(self.today, "R1")
        items = self.by_reference(run)
        self.assertEqual(set(items), {"GLX-4711", "UMB-0815", "INI-2026-17"})
        self.assertEqual((items["INI-2026-17"].status, items["INI-2026-17"].reason),
                         ("rejected", CLOSED))
        self.assertEqual(items["GLX-4711"].status, "accepted")
        self.assertEqual(items["UMB-0815"].status, "accepted")
        # The EndToEndId the bank holds is the supplier's invoice number, and
        # each payment went to the account its invoice names.
        paid = control(BANK, "GET", "/_mock/payments/GLX-4711")
        self.assertEqual(paid["creditor_iban"], ACCOUNTS[GLOBEX])
        self.assertEqual(paid["amount"], 119000)

    def test_the_same_run_twice_is_dupl_and_pays_nothing_twice(self):
        self.post_three()
        first = self.payments.run(self.today, "R1")
        paid = control(BANK, "GET", "/_mock/payments")
        balance = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        # Nothing clears an open item until the statement is posted back to
        # SAP (mock-bank#47), so the same run selects the same items again.
        again = self.payments.run(self.today, "R1")
        self.assertEqual(again.msg_id, first.msg_id)
        self.assertTrue(again.duplicate)
        self.assertEqual(control(BANK, "GET", "/_mock/payments"), paid)
        self.assertEqual(control(BANK, "GET", "/_mock/accounts/ACME")["balance"], balance)
        # The refusal of the copy does not undo what the first file did.
        self.assertEqual(self.by_reference(first)["GLX-4711"].status, "accepted")
        self.assertNotIn("rejected", {i.status for i in again.items
                                      if i.reference != "INI-2026-17"})

    def test_two_runs_on_one_day_are_two_files(self):
        # R1 and H shared a NACHA file ID modifier before the header carried
        # the identification exactly: the second run was refused as DUPL and
        # its invoice never paid.
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        first = self.payments.run(self.today, "R1")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
        second = self.payments.run(self.today, "H")
        self.assertNotEqual(second.msg_id, first.msg_id)
        self.assertFalse(second.duplicate)
        self.assertEqual(self.by_reference(second)["UMB-0815"].status, "accepted")
        self.assertEqual(control(BANK, "GET", "/_mock/payments/UMB-0815")["amount"], 23800)

    @unittest.skipUnless(NACHA, "what a BAI2 statement cannot carry back")
    def test_a_reference_the_statement_cannot_carry_back_is_skipped(self):
        """mock-bank#171, mock-bank#164 item 2: the entry carries it; the statement cannot.

        BAI2 has no escape character, so `bai2._safe` turns a `,` or a `/` in the
        reference into a space on the way back, and SAP matches the structured
        reference exactly. The payment is never cleared, the item stays open, and
        the next run pays it again - a double payment with nothing in either log
        saying anything went wrong. Skipping it is the only honest answer the run
        can give, because the run cannot change what the statement carries.
        """
        self.sap.invoice(GLOBEX, "GLX,4711", "10.00")          # a comma
        self.sap.invoice(GLOBEX, "GLX/4712", "20.00")          # a slash
        self.sap.invoice(GLOBEX, "GLX-4713", "5.00")           # neither
        items = self.by_reference(self.payments.run(self.today, "R1"))
        self.assertEqual({r: i.status for r, i in items.items()},
                         {"GLX,4711": "skipped", "GLX/4712": "skipped",
                          "GLX-4713": "accepted"})
        # The reason names the character, so a reader knows what to change.
        self.assertIn("BAI2 statement cannot carry back", items["GLX,4711"].reason)
        self.assertIn(repr(","), items["GLX,4711"].reason)
        self.assertIn(repr("/"), items["GLX/4712"].reason)
        # Neither reached the bank, so neither can be paid and left unmatched.
        for reference in ("GLX,4711", "GLX/4712"):
            status, _body = call(BANK, "GET", "/_mock/payments/"
                                 + urllib.parse.quote(reference, safe=""))
            self.assertEqual(status, 404, reference)

    @unittest.skipUnless(NACHA, "what a NACHA entry cannot carry")
    def test_what_a_nacha_entry_cannot_carry_is_skipped_and_the_rest_paid(self):
        self.sap.invoice(GLOBEX, "GLX 4711", "10.00")          # a space
        self.sap.invoice(GLOBEX, "GLX-BIG", "100000000.00")    # eleven digits of cents
        self.sap.domestic_bank(UMBRELLA, "021000021", "1" * 18)
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")       # eighteen-digit account
        self.sap.invoice(GLOBEX, "GLX-4712", "5.00")
        items = self.by_reference(self.payments.run(self.today, "R1"))
        self.assertEqual({r: i.status for r, i in items.items()},
                         {"GLX 4711": "skipped", "GLX-BIG": "skipped",
                          "UMB-0815": "skipped", "GLX-4712": "accepted"})
        self.assertIn("17 characters", items["UMB-0815"].reason)
        status, _body = call(BANK, "GET", "/_mock/payments/UMB-0815")
        self.assertEqual(status, 404)

    @unittest.skipUnless(NACHA, "what a NACHA record cannot hold")
    def test_a_control_character_in_a_name_skips_the_item_and_the_rest_is_paid(self):
        # ASCII, and not printable: a tab would sit in the record as it is, and
        # a line feed would end the record in the middle of the name.
        for character in ("\t", "\n", "\x7f"):
            with self.subTest(character=character):
                self.setUp()
                self.sap.holder(UMBRELLA, "Umbrella%sCorp" % character)
                self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
                self.sap.invoice(GLOBEX, "GLX-4711", "5.00")
                run = self.payments.run(self.today, "R1")
                items = self.by_reference(run)
                self.assertEqual({r: i.status for r, i in items.items()},
                                 {"UMB-0815": "skipped", "GLX-4711": "accepted"},
                                 [i.reason for i in run.items] + run.problems)
                self.assertIn("printable ASCII", items["UMB-0815"].reason)
                self.assertIn(repr(character), items["UMB-0815"].reason)
                status, _body = call(BANK, "GET", "/_mock/payments/UMB-0815")
                self.assertEqual(status, 404)

    @unittest.skipUnless(NACHA, "what a NACHA record cannot hold")
    def test_a_control_character_in_an_account_or_reference_skips_the_item(self):
        self.sap.domestic_bank(UMBRELLA, "021000021", "12345\t678")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
        self.sap.invoice(GLOBEX, "GLX&#127;4711", "10.00")     # DEL, as XML writes it
        self.sap.invoice(GLOBEX, "GLX-4712", "5.00")
        run = self.payments.run(self.today, "R1")
        items = self.by_reference(run)
        self.assertEqual({r: i.status for r, i in items.items()},
                         {"UMB-0815": "skipped", "GLX\x7f4711": "skipped",
                          "GLX-4712": "accepted"},
                         [i.reason for i in run.items] + run.problems)
        self.assertIn("account number", items["UMB-0815"].reason)
        self.assertIn("it holds %r" % "\t", items["UMB-0815"].reason)
        self.assertIn("it holds %r" % "\x7f", items["GLX\x7f4711"].reason)

    @unittest.skipUnless(NACHA, "what a NACHA record cannot hold")
    def test_a_routing_number_that_is_not_nine_digits_skips_the_item(self):
        # The second and third are digits to `str.isdigit()` and to nobody else.
        for routing in ("02100002", "02100002\u00b2",
                        "\u0660\u0662\u0661\u0660\u0660\u0660\u0660\u0662\u0661",
                        "0210000210", "02100002X"):
            with self.subTest(routing=routing):
                self.setUp()
                self.sap.domestic_bank(UMBRELLA, routing, "12345")
                self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
                self.sap.invoice(GLOBEX, "GLX-4711", "5.00")
                run = self.payments.run(self.today, "R1")
                items = self.by_reference(run)
                self.assertEqual({r: i.status for r, i in items.items()},
                                 {"UMB-0815": "skipped", "GLX-4711": "accepted"},
                                 [i.reason for i in run.items] + run.problems)
                self.assertIn("routing number %r" % routing, items["UMB-0815"].reason)
                self.assertIn("is not nine digits", items["UMB-0815"].reason)
                status, _body = call(BANK, "GET", "/_mock/payments/UMB-0815")
                self.assertEqual(status, 404)

    @unittest.skipUnless(NACHA, "what the bank does with a wrong check digit")
    def test_a_wrong_check_digit_is_left_to_the_bank_which_refuses_that_entry(self):
        self.sap.domestic_bank(UMBRELLA, "021000022", "12345")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
        self.sap.invoice(GLOBEX, "GLX-4711", "5.00")
        items = self.by_reference(self.payments.run(self.today, "R1"))
        self.assertEqual({r: (i.status, i.reason) for r, i in items.items()},
                         {"UMB-0815": ("rejected", "R03"), "GLX-4711": ("accepted", "")})

    def test_an_item_not_yet_due_is_not_selected(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(GLOBEX, "GLX-4712", "50.00", terms="NT30")
        run = self.payments.run(self.today, "R1")
        self.assertEqual([i.reference for i in run.items], ["GLX-4711"])
        status, _body = call(BANK, "GET", "/_mock/payments/GLX-4712")
        self.assertEqual(status, 404)

    def test_the_selection_asks_sap_to_leave_blocked_and_cleared_items_out(self):
        self.payments.select(self.today)
        asked = [urllib.parse.unquote(row["query"]) for row in
                 control(SAP, "GET", "/_mock/requests")["results"]
                 if row["path"].endswith("A_OperationalAcctgDocItemCube")]
        self.assertTrue(asked and OPEN_SUPPLIER_ITEMS in asked[0], asked)

    def test_a_blocked_invoice_is_never_selected(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.block(self.sap.invoice(UMBRELLA, "UMB-0815", "238.00"))
        run = self.payments.run(self.today, "R1")
        self.assertEqual([i.reference for i in run.items], ["GLX-4711"])
        status, _body = call(BANK, "GET", "/_mock/payments/UMB-0815")
        self.assertEqual(status, 404)


class StatementCase(MocksCase):
    """Bank time pinned to a Friday after the cutoff, and SAP's items to look at."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.today.weekday(), 4, "start mock-bank with "
                         "--clock 2026-10-02T16:00, a Friday after the cutoff")
        self.monday = self.today + datetime.timedelta(days=3)

    def advance(self, to):
        control(BANK, "POST", "/_mock/advance?to=%s" % to.isoformat())

    def post_two(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")

    def cube_item(self, reference):
        """SAP's open-item line for a supplier invoice number."""
        invoice = odata(SAP, ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice",
                        **{"$filter": "SupplierInvoiceIDByInvcgParty eq '%s'" % reference})[0]
        return odata(SAP, ITEMS, **{"$filter": (
            "AccountingDocument eq '%s' and AccountingDocumentItemType eq 'K'"
            % invoice["AccountingDocument"])})[0]

    def clearing(self, reference):
        """The clearing document on that line; "" while it is open."""
        return self.cube_item(reference)["ClearingAccountingDocument"]

    def pay_on_monday(self):
        """A run on Monday morning, before the cutoff: it settles that day."""
        self.advance(self.monday)
        run = self.payments.run(self.monday, "R1")
        self.advance(self.monday + datetime.timedelta(days=1))
        return run


class ReconcilingTheStatement(StatementCase):
    """mock-bank#47: each camt.053 posted to SAP as a FINSTA01, clearing what it paid."""

    def test_1_a_clean_run_is_paid_matched_and_cleared(self):
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        monday = [s for s in run.statements if s["date"] == self.monday.isoformat()]
        self.assertEqual(len(monday), 1, run.statements)
        self.assertTrue(monday[0]["adds_up"])
        self.assertEqual(monday[0]["findings"], [])
        self.assertEqual({i.reference: i.status for i in run.items},
                         {"GLX-4711": "cleared", "UMB-0815": "cleared"})
        for reference in ("GLX-4711", "UMB-0815"):
            self.assertNotEqual(self.clearing(reference), "", reference)
        # Cleared in SAP, so the next run has nothing left to pay.
        self.assertEqual(self.payments.select(self.monday), [])

    def test_5_a_statement_gap_leaves_the_missing_payment_unreconciled_and_open(self):
        control(BANK, "PATCH", "/_mock/accounts/ACME", {"behaviour": "statement-gap"})
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        monday = [s for s in run.statements if s["date"] == self.monday.isoformat()][0]
        self.assertFalse(monday["adds_up"])
        # SAP's own arithmetic check says so too, in its own words.
        self.assertTrue(any("does not add up" in f for f in monday["findings"]),
                        monday["findings"])
        statuses = sorted(i.status for i in run.items)
        self.assertEqual(statuses, ["cleared", "unreconciled"])
        missing = [i for i in run.items if i.status == "unreconciled"][0]
        self.assertIn("short", missing.reason)
        self.assertEqual(self.clearing(missing.reference), "")

    def test_6_after_the_cutoff_it_waits_for_mondays_statement(self):
        self.post_two()
        run = self.payments.run(self.today, "R1")        # Friday, 16:00
        self.advance(self.today + datetime.timedelta(days=1))
        self.payments.reconcile(run)
        friday = [s for s in run.statements if s["date"] == self.today.isoformat()]
        self.assertEqual(len(friday), 1, run.statements)
        # An empty day's statement posts harmlessly: nothing cleared, nothing
        # it could not place, nothing wrong with it.
        self.assertEqual((friday[0]["cleared"], friday[0]["unprocessed"],
                          friday[0]["findings"]), ([], [], []))
        self.assertEqual({i.status for i in run.items}, {"accepted"})
        self.advance(self.monday + datetime.timedelta(days=1))
        self.payments.reconcile(run)
        self.assertEqual({i.status for i in run.items}, {"cleared"})

    def test_posting_the_same_statement_twice_clears_nothing_twice(self):
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        monday = [s for s in run.statements if s["date"] == self.monday.isoformat()][0]
        before = {r: self.clearing(r) for r in ("GLX-4711", "UMB-0815")}
        again = self.payments.session.post_idoc(monday["finsta"])
        self.assertEqual(again.get("CLEARED"), [])
        self.assertEqual(again.get("REOPENED"), [])
        self.assertEqual({r: self.clearing(r) for r in before}, before)


class AReturnedPayment(StatementCase):
    """mock-bank#48: a return on a later statement reopens the invoice it paid."""

    def test_3_a_return_reopens_the_invoice_distinguishable_from_one_never_paid(self):
        control(BANK, "PATCH", "/_mock/accounts/ACME", {
            "behaviour": "return-later",
            "parameters": {"end_to_end_id": "GLX-4711", "days": 3, "reason": CLOSED}})
        self.post_three()
        run = self.pay_on_monday()               # INITECH is rejected AC04: never paid
        self.payments.reconcile(run)
        self.assertEqual(self.by_reference(run)["GLX-4711"].status, "cleared")
        # Three business days after Monday's settlement is Thursday; Friday
        # morning, Thursday's statement is in.
        self.advance(self.monday + datetime.timedelta(days=4))
        self.payments.reconcile(run)
        items = self.by_reference(run)
        self.assertEqual(items["GLX-4711"].status, "returned")
        self.assertTrue(items["GLX-4711"].reason.startswith(CLOSED), items["GLX-4711"].reason)
        self.assertEqual(items["UMB-0815"].status, "cleared")
        self.assertEqual(items["INI-2026-17"].status, "rejected")
        # In SAP: returned and never paid are both open, and only one was paid.
        returned, never = self.cube_item("GLX-4711"), self.cube_item("INI-2026-17")
        self.assertEqual((returned["ClearingAccountingDocument"], never["ClearingAccountingDocument"]),
                         ("", ""))
        self.assertEqual((returned["ClearingIsReversed"], never["ClearingIsReversed"]),
                         (True, False))
        self.assertNotEqual(self.cube_item("UMB-0815")["ClearingAccountingDocument"], "")
        # Owed again, so the next run selects it with the one never paid.
        self.assertEqual(sorted(i.reference for i in self.payments.select(self.bank_today())),
                         ["GLX-4711", "INI-2026-17"])


class MoneyArriving(StatementCase):
    """#2: a credit that quotes a paid invoice is not that payment coming back.

    A customer pays ACME and quotes, by mistake or by coincidence, the number of
    an invoice ACME has already paid a supplier. On the statement it is a
    credit with that reference, which is also what a returned payment is. Read
    as a return, the invoice reopens and the next run pays the supplier twice.
    """

    def receive(self, reference, minor_units):
        return control(BANK, "POST", "/_mock/credits", {
            "account": "ACME", "amount": minor_units, "currency": CURRENCY,
            "end_to_end_id": reference, "note": reference,
            "debtor": {"name": "A Customer Ltd", "iban": "DE89370400440532013000",
                       "bic": "COBADEFFXXX"}})

    def test_a_credit_quoting_a_paid_invoice_leaves_it_paid(self):
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        cleared_by = self.clearing("GLX-4711")
        self.assertNotEqual(cleared_by, "")
        credit = self.receive("GLX-4711", 119000)
        self.advance(datetime.date.fromisoformat(credit["booking_date"])
                     + datetime.timedelta(days=1))
        self.payments.reconcile(run)
        # The credit reached SAP: the statement it is on adds up, and nothing
        # on it was taken for a return.
        carrying = [s for s in run.statements if s["date"] == credit["booking_date"]]
        self.assertEqual(len(carrying), 1)
        self.assertTrue(carrying[0]["adds_up"])
        self.assertIn("<MOABETR>1190.00</MOABETR>", carrying[0]["finsta"])
        self.assertEqual(carrying[0]["reopened"], [])
        # Still paid, in SAP and in the run, and not owed to the next run.
        self.assertEqual(self.clearing("GLX-4711"), cleared_by)
        self.assertEqual(self.by_reference(run)["GLX-4711"].status, "cleared")
        self.assertEqual(self.payments.select(self.bank_today()), [])
        # SAP was given the reference, and it is what the credit says it is
        # that kept the invoice paid (#18): SAP's own answer is that the line
        # is money arriving, which it does not post yet (mock-sap#65).
        self.assertIn("<LINACTION>RCV</LINACTION>", carrying[0]["finsta"])
        self.assertEqual(carrying[0]["finsta"].count("<BELNR>GLX-4711</BELNR>"), 1)
        self.assertEqual(len(carrying[0]["unprocessed"]), 1, carrying[0])
        self.assertIn("money arriving", carrying[0]["unprocessed"][0]["REASON"])
        # Nothing was held back, so the run has no problem to report for it.
        self.assertEqual([p for p in run.problems if "GLX-4711" in p], [])

    def test_a_return_beside_money_arriving_still_reopens_its_invoice(self):
        # The other half: telling the two apart must not stop a real return.
        control(BANK, "PATCH", "/_mock/accounts/ACME", {
            "behaviour": "return-later",
            "parameters": {"end_to_end_id": "UMB-0815", "days": 3, "reason": CLOSED}})
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        self.receive("GLX-4711", 119000)
        self.advance(self.monday + datetime.timedelta(days=4))
        self.payments.reconcile(run)
        items = self.by_reference(run)
        self.assertEqual((items["GLX-4711"].status, items["UMB-0815"].status),
                         ("cleared", "returned"))
        # And each said which it was, on the statement SAP was given.
        written = "".join(statement["finsta"] for statement in run.statements)
        self.assertEqual(sorted(re.findall(r"<LINACTION>(\w+)</LINACTION>", written)),
                         ["RCV", "RET"])
        self.assertTrue(items["UMB-0815"].reason.startswith(CLOSED), items["UMB-0815"].reason)
        self.assertEqual([i.reference for i in self.payments.select(self.bank_today())],
                         ["UMB-0815"])


class WhatACreditSaysItIs(unittest.TestCase):
    """#18: each credit on the FINSTA01 declares itself a return or a receipt.

    The sign says money came in and nothing more. mock-sap reads the
    declaration from 0.19.0, and there a credit that makes none reverses
    nothing - so a return that did not say so would stop reopening its
    invoice. With no mock running: this is what is written.
    """

    def lines(self):
        finsta = PaymentRun(SAP, BANK, ACME, MODE).finsta(
            "7", "2026-10-08", Decimal("100.00"), Decimal("110.00"), [
                {"end_to_end_id": "OUT-1", "amount": "10.00", "side": "DBIT",
                 "returned": False},
                {"end_to_end_id": "BACK-1", "amount": "15.00", "side": "CRDT",
                 "returned": True},
                {"end_to_end_id": "IN-1", "amount": "5.00", "side": "CRDT",
                 "returned": False}], CURRENCY)
        return re.findall(r"<E1IDPF1 SEGMENT=\"1\">(.*?)</E1IDPF1>", finsta)[:3]

    def test_a_return_says_ret_and_keeps_the_reference_sap_reopens_by(self):
        _, back, _ = self.lines()
        self.assertIn("<LINACTION>RET</LINACTION>", back)
        self.assertIn("<BELNR>BACK-1</BELNR>", back)

    def test_money_arriving_says_rcv_and_keeps_its_reference(self):
        _, _, arrived = self.lines()
        self.assertIn("<LINACTION>RCV</LINACTION>", arrived)
        # The reference is what SAP will clear a receivable by (mock-sap#65).
        # It was left off until a mock-sap that reads the declaration was out.
        self.assertIn("<BELNR>IN-1</BELNR>", arrived)

    def test_a_debit_says_nothing_because_it_has_one_reading(self):
        out, _, _ = self.lines()
        self.assertNotIn("LINACTION", out)
        self.assertIn("<BELNR>OUT-1</BELNR>", out)

    def test_the_declaration_comes_before_the_segments_inside_the_line(self):
        """A field of the segment, so it is written with the segment's fields."""
        for line in self.lines()[1:]:
            self.assertTrue(re.match(r"<LINLINEIT>\d{6}</LINLINEIT><LINACTION>", line), line)


class TheStatementsCurrency(StatementCase):
    """#2: the FINSTA01 says the currency the statement is in, not `EUR`."""

    def test_every_amount_and_the_account_carry_the_accounts_currency(self):
        self.post_two()
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        self.assertTrue(run.statements)
        for statement in run.statements:
            written = set(re.findall(r"<(?:CUXWAERZ|FIIKWAER)>([^<]*)<", statement["finsta"]))
            self.assertEqual(written, {CURRENCY}, statement["number"])
        # Nothing was lost by saying so: both invoices cleared.
        self.assertEqual({i.status for i in run.items}, {"cleared"})

    def test_a_statement_that_names_no_currency_is_not_posted(self):
        camt = ('<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:'
                'camt.053.001.08"><BkToCstmrStmt><Stmt><ElctrncSeqNb>7</ElctrncSeqNb>'
                '<FrToDt><FrDtTm>2026-10-05T00:00:00+00:00</FrDtTm></FrToDt>'
                '<Acct><Id><IBAN>%s</IBAN></Id></Acct>%s</Stmt></BkToCstmrStmt></Document>'
                % (ACME["iban"], "".join(
                    '<Bal><Tp><CdOrPrtry><Cd>%s</Cd></CdOrPrtry></Tp><Amt>10.00</Amt>'
                    '<CdtDbtInd>CRDT</CdtDbtInd></Bal>' % code for code in ("OPBD", "CLBD"))))
        run = Run(datetime.date(2026, 10, 5), "R1", [])
        runner = PaymentRun(SAP, BANK, ACME, "iso20022")
        with unittest.mock.patch.object(payment_run_module, "call",
                                        return_value=(200, camt.encode())), \
                unittest.mock.patch.object(runner.session, "post_idoc") as posted:
            runner.reconcile(run)
        posted.assert_not_called()
        self.assertEqual(run.statements, [])
        self.assertEqual(len(run.problems), 1)
        self.assertIn("does not say what currency", run.problems[0])


class ReadingABai2Statement(unittest.TestCase):
    """What `bai2_statements` takes from a file, with no mock running."""

    def read(self, group_currency, account_currency, *movements):
        text = "\n".join(
            ["01,MOCKBANK,ACME,261005,0000,1,80,1,2/",
             "02,ACME,MOCKBANK,1,261005,0000,%s,2/" % group_currency,
             "03,0000000001,%s,010,10000,,,015,10000,,/" % account_currency]
            + ["16,%s,%s,0,%s,MSG-1,text/" % m for m in movements]
            + ["49,20000,3/", "98,20000,1,5/", "99,20000,1,7/"])
        return payment_run_module.bai2_statements(text)[0]

    def test_the_currency_is_the_accounts_then_the_groups_then_dollars(self):
        self.assertEqual(self.read("CAD", "GBP")["currency"], "GBP")
        self.assertEqual(self.read("CAD", "")["currency"], "CAD")
        self.assertEqual(self.read("", "")["currency"], "USD")

    def test_only_an_individual_ach_return_item_is_a_payment_coming_back(self):
        lines = self.read("USD", "USD", ("257", "100", "BACK-1"), ("142", "200", "IN-1"),
                          ("447", "300", "OUT-1"))["lines"]
        self.assertEqual([(l["end_to_end_id"], l["side"], l["returned"]) for l in lines],
                         [("BACK-1", "CRDT", True), ("IN-1", "CRDT", False),
                          ("OUT-1", "DBIT", False)])


    def test_a_movement_with_no_reference_is_read_with_an_empty_one(self):
        """#44: BAI2 lets a `16` record stop after the funds type. It is not an
        error, and nothing is made up for it: the line reaches SAP quoting
        nothing."""
        text = "\n".join(
            ["01,MOCKBANK,ACME,261005,0000,1,80,1,2/",
             "02,ACME,MOCKBANK,1,261005,0000,USD,2/",
             "03,0000000001,USD,010,10000,,,015,9700,,/",
             "16,447,300,0/",
             "49,20000,3/", "98,20000,1,5/", "99,20000,1,7/"])
        [line] = payment_run_module.bai2_statements(text)[0]["lines"]
        self.assertEqual((line["end_to_end_id"], line["msg_id"], line["side"]),
                         ("", "", "DBIT"))


class ASecondRunBeforeTheStatement(StatementCase):
    """#2, #21: an item one run has sent to the bank is not paid again by the next.

    Nothing clears an open item until the statement is posted. What SAP can
    say in the meantime is which payment run has it (mock-sap#90), so the run
    writes that on the invoice before its file goes, and these hold it to what
    that promises: one payment, and the item free again exactly when the bank
    refuses it, SAP clears it, or the payment comes back.

    Every second run here is a `PaymentRun` of its own with nothing handed to
    it, which is a payment program started again, or somebody else's.
    """

    def spent(self, before):
        return before - control(BANK, "GET", "/_mock/accounts/ACME")["balance"]

    def another(self):
        return PaymentRun(SAP, BANK, ACME, MODE)

    def claim(self, reference):
        row = self.cube_item(reference)
        day = sap_date(row["PaymentRunDate"])
        return row["PaymentRunID"], day.isoformat() if day else ""

    def test_the_invoice_is_paid_once(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        before = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        first = self.payments.run(self.monday, "R1")
        second = self.another().run(self.monday, "R2")
        self.assertEqual([i.status for i in first.items], ["accepted"])
        [held] = second.items
        self.assertEqual(held.status, "skipped")
        # It says which run has it, so that somebody reading the second run's
        # result is not left wondering why a due invoice was not paid.
        self.assertIn("payment run R1 of %s" % self.monday.isoformat(), held.reason)
        self.assertIsNone(second.http_status, "nothing to pay is no file sent")
        self.advance(self.monday + datetime.timedelta(days=1))
        self.assertEqual(self.spent(before), 119000)

    def test_the_open_item_in_sap_says_which_run_has_it(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.assertEqual(self.claim("GLX-4711"), ("", ""))
        self.advance(self.monday)
        [item] = self.payments.run(self.monday, "R1").items
        self.assertEqual(self.claim("GLX-4711"), ("R1", self.monday.isoformat()))
        self.assertEqual((item.run_id, item.run_date), ("R1", self.monday.isoformat()))

    def test_the_claim_is_in_sap_before_the_file_reaches_the_bank(self):
        """Write-ahead: a crash after sending must not find the item unclaimed."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        seen = []

        def sending(base, method, path, body=None, content_type=""):
            if path == "/payments":
                seen.append(self.claim("GLX-4711"))
            return call(base, method, path, body, content_type)

        with unittest.mock.patch.object(payment_run_module, "call", sending):
            self.payments.run(self.monday, "R1")
        self.assertEqual(seen, [("R1", self.monday.isoformat())])

    def unreadable(self):
        """A run whose file the bank cannot read far enough to name."""
        runner = self.another()
        writer = "nacha_file" if NACHA else "payment_file"
        return unittest.mock.patch.object(
            runner, writer, return_value="101 short\n" if NACHA else "<not xml"), runner

    def test_a_file_refused_with_nothing_naming_the_run_is_refused_and_let_go(self):
        """#30: it was left `sent` and claimed, for a payment that did not exist."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00")
        self.advance(self.monday)
        before = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        patched, runner = self.unreadable()
        with patched:
            run = runner.run(self.monday, "R1")
        self.assertEqual(run.http_status, 422)
        self.assertEqual({i.reference: (i.status, i.reason) for i in run.items},
                         {"GLX-4711": ("rejected", "FF01"), "UMB-0815": ("rejected", "FF01")})
        self.assertEqual(len(run.problems), 1, run.problems)
        self.assertIn("refused the payment file with FF01", run.problems[0])
        self.assertIn("none of its 2 payments was made", run.problems[0])
        self.assertFalse(run.duplicate)
        # The bank's own words for what was wrong are in the problem.
        self.assertIn(run.refused[1], run.problems[0])
        self.assertTrue(run.refused[1])
        self.assertEqual(self.claim("GLX-4711"), ("", ""))
        self.assertEqual(self.claim("UMB-0815"), ("", ""))
        self.assertEqual(self.spent(before), 0)
        # So the next run pays them, once.
        again = self.another().run(self.monday, "R2")
        self.assertEqual({i.reference: i.status for i in again.items},
                         {"GLX-4711": "accepted", "UMB-0815": "accepted"})

    def test_a_copy_refused_before_its_report_arrives_is_still_a_copy(self):
        """The answer to the file says `DUPL` too. Read as a refusal, it would
        take the claim off an invoice the first file paid."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        self.payments.run(self.monday, "R1")

        def no_reports(base, method, path, body=None, content_type=""):
            if path.startswith("/_mock/mailbox"):
                return 200, b""
            return call(base, method, path, body, content_type)

        with unittest.mock.patch.object(payment_run_module, "call", no_reports):
            copy = self.another().run(self.monday, "R1")
        self.assertEqual(copy.http_status, 422)
        self.assertTrue(copy.duplicate)
        self.assertEqual([(i.status, i.reason) for i in copy.items], [("sent", "")])
        self.assertEqual(copy.problems, [])
        self.assertEqual(self.claim("GLX-4711"), ("R1", self.monday.isoformat()))

    def test_a_refusal_that_cannot_be_read_leaves_the_items_held_and_says_so(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        for body in (b"Unprocessable", b"[]", b'{"status": "RJCT"}',
                     b'{"status": "RJCT", "reason": 5}',
                     b'{"status": "ACCP", "reason": "FF01"}', b"\xff"):
            with self.subTest(body=body):
                def answering(base, method, path, sent=None, content_type=""):
                    if path == "/payments":
                        return 422, body
                    if path.startswith("/_mock/mailbox"):
                        return 200, b""
                    return call(base, method, path, sent, content_type)

                with unittest.mock.patch.object(payment_run_module, "call", answering):
                    run = self.another().run(self.monday, "R1")
                self.assertEqual([i.status for i in run.items], ["sent"])
                self.assertEqual(len(run.problems), 1, run.problems)
                self.assertIn("what it said could not be read", run.problems[0])
                self.assertEqual(self.claim("GLX-4711"), ("R1", self.monday.isoformat()))

    def test_a_claim_somebody_else_wrote_is_left_alone_too(self):
        """Not only this code's runs: any payment program that says so in SAP."""
        posted = self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.sap.write("PATCH", ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
                       "(SupplierInvoice='%s',FiscalYear='%s')"
                       % (posted["SUPPLIERINVOICE"], posted["FISCALYEAR"]),
                       json.dumps({"PaymentRunID": "OTHER", "PaymentRunDate": None}),
                       "application/json")
        self.advance(self.monday)
        [held] = self.payments.run(self.monday, "R1").items
        self.assertEqual(held.status, "skipped")
        self.assertIn("payment run OTHER of no date", held.reason)
        self.assertEqual(self.claim("GLX-4711"), ("OTHER", ""))

    def test_the_same_identification_on_another_day_is_another_run(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        self.payments.run(self.monday, "R1")
        tuesday = self.monday + datetime.timedelta(days=1)
        [held] = self.another().run(tuesday, "R1").items
        self.assertEqual(held.status, "skipped")

    def test_an_item_nobody_has_sent_is_still_paid_by_the_second_run(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        self.payments.run(self.monday, "R1")
        self.sap.invoice(UMBRELLA, "UMB-0815", "238.00", dated=self.today)
        second = self.another().run(self.monday, "R2")
        self.assertEqual({r: i.status for r, i in self.by_reference(second).items()},
                         {"GLX-4711": "skipped", "UMB-0815": "accepted"})

    def test_the_same_run_again_is_not_held_back_from_its_own_items(self):
        """It is sent, and the bank refuses the copy: what the run promised before."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        before = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        self.payments.run(self.monday, "R1")
        again = self.another().run(self.monday, "R1")
        self.assertTrue(again.duplicate)
        self.assertEqual([i.status for i in again.items], ["sent"])
        self.advance(self.monday + datetime.timedelta(days=1))
        self.assertEqual(self.spent(before), 119000)

    def test_once_sap_has_cleared_it_sap_has_let_go(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.pay_on_monday()
        self.assertEqual(self.claim("GLX-4711")[0], "R1",
                         "accepted is not paid: still held until the statement")
        self.payments.reconcile(run)
        self.assertEqual(run.items[0].status, "cleared")
        self.assertEqual(self.claim("GLX-4711"), ("", ""))

    def test_a_payment_the_bank_refused_is_free_for_the_next_run(self):
        self.sap.invoice(INITECH, "INI-2026-17", "595.00")
        self.advance(self.monday)
        first = self.payments.run(self.monday, "R1")
        [refused] = first.items
        self.assertEqual((refused.status, refused.reason), ("rejected", CLOSED))
        # SAP cannot see a refusal, so the run took its own claim off.
        self.assertEqual(self.claim("INI-2026-17"), ("", ""))
        self.assertEqual((refused.run_id, first.problems), ("", []))
        second = self.another().run(self.monday, "R2")
        # Sent again and refused again, which is right: the item is owed, and
        # it is the vendor master that is wrong, not the run.
        self.assertEqual([(i.status, i.reason) for i in second.items],
                         [("rejected", CLOSED)])

    def test_a_refusal_sap_was_not_told_of_is_said_and_the_item_stays_held(self):
        self.sap.invoice(INITECH, "INI-2026-17", "595.00")
        self.advance(self.monday)
        taking_off = self.payments.write_claim

        def refused(item, identification, date):
            if not identification:
                return "SAP answered 503 to the payment run on invoice X"
            return taking_off(item, identification, date)

        with unittest.mock.patch.object(self.payments, "write_claim", refused):
            first = self.payments.run(self.monday, "R1")
        self.assertEqual(first.items[0].status, "rejected")
        [said] = first.problems
        self.assertIn("INI-2026-17", said)
        self.assertIn("still marked in SAP as in payment with run R1", said)
        self.assertEqual([i.status for i in self.another().run(self.monday, "R2").items],
                         ["skipped"])

    def test_a_payment_that_came_back_is_paid_again_by_the_next_run(self):
        control(BANK, "PATCH", "/_mock/accounts/ACME", {
            "behaviour": "return-later",
            "parameters": {"end_to_end_id": "GLX-4711", "days": 3, "reason": CLOSED}})
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.pay_on_monday()
        self.payments.reconcile(run)
        friday = self.monday + datetime.timedelta(days=4)
        self.advance(friday)
        self.payments.reconcile(run)
        self.assertEqual(run.items[0].status, "returned")
        self.assertEqual(self.claim("GLX-4711"), ("", ""))
        again = self.another().run(friday, "R2")
        self.assertEqual([i.status for i in again.items], ["accepted"])

    def test_a_bank_that_did_not_answer_leaves_the_item_with_the_run_that_tried(self):
        """A request that timed out may have arrived. Another run does not get to
        find out by paying; the run that tried sends its own file again."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        unheard = PaymentRun(SAP, closed_port(), ACME, MODE)
        first = unheard.run(self.monday, "R1")
        self.assertEqual([i.status for i in first.items], ["selected"])
        self.assertIn("did not answer", first.problems[0])
        other = self.another().run(self.monday, "R2")
        self.assertEqual([i.status for i in other.items], ["skipped"])
        same = self.another().run(self.monday, "R1")
        self.assertEqual([i.status for i in same.items], ["accepted"])

    def test_an_item_sap_would_not_take_the_claim_for_is_not_sent(self):
        """Paying it would be paying what no other run can see is in flight."""
        self.post_two()
        self.advance(self.monday)
        writing = self.payments.write_claim

        def one_refused(item, identification, date):
            if item.reference == "UMB-0815":
                return "SAP answered 500 to the payment run on invoice X"
            return writing(item, identification, date)

        with unittest.mock.patch.object(self.payments, "write_claim", one_refused):
            run = self.payments.run(self.monday, "R1")
        items = self.by_reference(run)
        self.assertEqual((items["GLX-4711"].status, items["UMB-0815"].status),
                         ("accepted", "skipped"))
        self.assertIn("not claimed in SAP, so not sent", items["UMB-0815"].reason)
        self.assertEqual(len([p for p in run.problems if "UMB-0815" in p]), 1)
        self.assertEqual(self.claim("UMB-0815"), ("", ""))
        self.assertEqual([m["end_to_end_id"] for m in
                          control(BANK, "GET", "/_mock/payments")], ["GLX-4711"])

    def test_sap_gone_between_the_selection_and_the_claim_sends_nothing(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        self.payments.session = SapSession(closed_port())
        run = self.payments.run(self.monday, "R1")
        self.assertEqual([i.status for i in run.items], ["skipped"])
        self.assertIsNone(run.http_status)
        self.assertIn("SAP did not answer", run.problems[0])

    def test_an_identification_saps_key_cannot_hold_is_refused(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        for identification in ("", "SEVENCH"):
            run = self.payments.run(self.monday, identification)
            self.assertEqual(run.items, [])
            self.assertIn("is not one to 6 characters", run.problems[0])
            self.assertIn("no open item was selected", run.problems[0])
        self.assertEqual(self.claim("GLX-4711"), ("", ""))


class TwoSuppliersOneNumberOneAmount(StatementCase):
    """#44: where mock-sap's reading of a statement line turns on this run's claim.

    An invoice number is a supplier's own sequence, so two suppliers can both
    bill `INV-1`, and for the same money. The bank's line quotes `INV-1` and an
    amount, both items fit, and nothing on the line says which supplier was
    paid. **From mock-sap 0.21.0** (mock-sap#173) the item a payment run has
    claimed is the one cleared, and the claim is the `PaymentRunID` this run
    writes before its file goes. Up to 0.20.0 neither was cleared and the line
    was answered under `UNPROCESSED`. So these assert mock-sap 0.21.0's answer,
    and it is this repository's own write that decides it.

    Against the mocks, with the bank's own statement: `TwoSuppliersWithOneInvoiceNumber`
    below hands SAP's answer over, and so says nothing of what SAP answers.
    """

    def test_the_item_the_run_claimed_is_the_one_cleared(self):
        self.sap.invoice(GLOBEX, "INV-1", "1190.00")
        self.advance(self.monday)
        run = self.payments.run(self.monday, "R1")
        self.assertEqual([i.status for i in run.items], ["accepted"])
        # Posted after the run, so no run has it: the same number and the same
        # amount, from another supplier.
        umbrella = self.sap.invoice(UMBRELLA, "INV-1", "1190.00")
        self.advance(self.monday + datetime.timedelta(days=1))
        self.payments.reconcile(run)
        [monday] = [s for s in run.statements if s["date"] == self.monday.isoformat()]
        self.assertEqual(monday["unprocessed"], [])
        [item] = run.items
        self.assertEqual(item.status, "cleared")
        open_now = {row["AccountingDocument"] for row in odata(
            SAP, ITEMS, **{"$filter": "AccountingDocumentItemType eq 'K' and "
                                      "ClearingAccountingDocument eq ''"})}
        self.assertNotIn(item.document.rsplit("/", 1)[-1], open_now)
        self.assertIn(umbrella["ACCOUNTINGDOCUMENT"], open_now)
        self.assertEqual(run.problems, [])

    def test_with_no_claim_on_either_neither_is_cleared(self):
        """The other half: without a claim SAP cannot tell, and does not pick."""
        globex = self.sap.invoice(GLOBEX, "INV-1", "1190.00")
        umbrella = self.sap.invoice(UMBRELLA, "INV-1", "1190.00")
        run = Run(self.monday, "R1", [])
        record = self.payments.post_statement(run, {
            "number": "900", "day": self.monday.isoformat(), "currency": CURRENCY,
            "opening": Decimal("5000.00"), "closing": Decimal("3810.00"),
            "lines": [{"end_to_end_id": "INV-1", "amount": Decimal("1190.00"),
                       "side": "DBIT", "returned_for": "", "returned": False}]})
        self.assertEqual(record["cleared"], [])
        [refused] = record["unprocessed"]
        for posted in (globex, umbrella):
            self.assertIn(posted["ACCOUNTINGDOCUMENT"], refused["REASON"])
        # Money left and SAP placed it nowhere: the run says so, in SAP's words
        # (#46). It is nobody's payment in this run, so no item is named.
        [problem] = run.problems
        self.assertTrue(problem.startswith(
            "statement 900 for %s, line 1: a debit of 1190.00 quoting INV-1 "
            "cleared nothing in SAP (" % self.monday.isoformat()), problem)
        self.assertTrue(problem.endswith("(%s)" % refused["REASON"]), problem)


class AStatementLineWithNoReference(StatementCase):
    """#44: a debit that quotes nothing clears nothing, whatever it is for.

    The reference on a FINSTA01 line is what the bank's statement gave back,
    and a bank need not give one back: a BAI2 `16` record may stop after the
    funds type. That line goes to SAP as an empty `BELNR`.

    mock-sap does **not** then match on the amount (0.21.0, `reconcile._match`):
    a line that names no reference is quoted by no item, and is answered under
    `UNPROCESSED` as "no open item quotes this reference" - with one open item
    of exactly that amount, and with this run's claim on it. #44 was raised
    expecting the claim to decide here; it does not, and this holds that down.

    So the payment is made and the item stays open and claimed, and the run
    says so as a problem (#46): which line, SAP's reason, and which of its own
    payments are for that amount.

    The statement is written here, not fetched: mock-bank always gives the
    reference back.
    """

    def test_the_only_item_of_that_amount_claimed_by_the_run_is_not_cleared(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        run = self.payments.run(self.monday, "R1")
        record = self.payments.post_statement(run, {
            "number": "900", "day": self.monday.isoformat(), "currency": CURRENCY,
            "opening": Decimal("5000.00"), "closing": Decimal("3810.00"),
            "lines": [{"end_to_end_id": "", "amount": Decimal("1190.00"),
                       "side": "DBIT", "returned_for": "", "returned": False}]})
        self.assertIn("<BELNR></BELNR>", record["finsta"])
        self.assertEqual(record["cleared"], [])
        self.assertEqual([u["REASON"] for u in record["unprocessed"]],
                         ["no open item quotes this reference"])
        self.assertEqual([i.status for i in run.items], ["accepted"])
        self.assertEqual(self.clearing("GLX-4711"), "")
        self.assertEqual(self.cube_item("GLX-4711")["PaymentRunID"], "R1")
        self.assertEqual(run.problems, [
            "statement 900 for %s, line 1: a debit of 1190.00 quoting no reference "
            "cleared nothing in SAP (no open item quotes this reference); this "
            "run's accepted payment of that amount: GLX-4711" % self.monday.isoformat()])


class WhatSapCouldNotPlace(unittest.TestCase):
    """#46: which of SAP's `UNPROCESSED` rows the run reports, and how.

    SAP's answer is handed over at `post_idoc`, so no mock has to be running.
    """

    def post(self, unprocessed, items=()):
        run = Run(datetime.date(2026, 10, 5), "R1", list(items))
        statement = {"number": "7", "day": "2026-10-05", "currency": CURRENCY,
                     "opening": Decimal("100.00"), "closing": Decimal("75.00"),
                     "lines": [{"end_to_end_id": "OUT-1", "amount": "10.00",
                                "side": "DBIT", "returned_for": "", "returned": False},
                               {"end_to_end_id": "IN-1", "amount": "5.00",
                                "side": "CRDT", "returned_for": "", "returned": False},
                               {"end_to_end_id": "", "amount": "20.00",
                                "side": "DBIT", "returned_for": "", "returned": False}]}
        runner = PaymentRun(SAP, BANK, ACME, MODE)
        with unittest.mock.patch.object(runner.session, "post_idoc",
                                        return_value={"UNPROCESSED": unprocessed}):
            runner.post_statement(run, statement)
        return run

    def item(self, reference, amount, status="accepted"):
        return Item(document="1000/2026/01%s" % reference[-1] * 8, supplier=GLOBEX,
                    reference=reference, amount=amount, status=status)

    def test_a_debit_is_a_problem_and_a_credit_is_not(self):
        run = self.post([{"LINE": "000001", "REASON": "because"},
                         {"LINE": "000002", "REASON": "money arriving"}])
        self.assertEqual(run.problems, [
            "statement 7 for 2026-10-05, line 1: a debit of 10.00 quoting OUT-1 "
            "cleared nothing in SAP (because)"])

    def test_every_accepted_payment_of_that_amount_is_named_and_none_picked(self):
        items = [self.item("A-1", "20.00"), self.item("A-2", "20.00"),
                 self.item("A-3", "20.00", status="cleared"), self.item("A-4", "10.00")]
        run = self.post([{"LINE": 3}], items)
        self.assertEqual(run.problems, [
            "statement 7 for 2026-10-05, line 3: a debit of 20.00 quoting no "
            "reference cleared nothing in SAP (no reason given); this run's "
            "accepted payments of that amount: A-1, A-2"])
        self.assertEqual([i.status for i in items],
                         ["accepted", "accepted", "cleared", "accepted"])

    def test_a_row_that_names_no_line_of_the_statement_is_left_on_the_record(self):
        # The fourth line is the totals this run wrote itself; the others name
        # nothing. None of them is a movement the run can say anything about.
        run = self.post([{"LINE": "000004"}, {"LINE": "x"}, {"REASON": "?"},
                         {"LINE": "000009"}, {"LINE": "000000"}])
        self.assertEqual(run.problems, [])
        self.assertEqual(len(run.statements[0]["unprocessed"]), 5)


class TheRunsOwnRegister(StatementCase):
    """`Register`, for a caller that still passes one: a second record, its own.

    It is asked after SAP, and let go of by this code where SAP lets go of its
    own. With the claim taken off the invoice in SAP by hand, the register is
    all that holds the item - the one thing it adds.
    """

    def setUp(self):
        super().setUp()
        self.register = Register()
        self.payments = PaymentRun(SAP, BANK, ACME, MODE, register=self.register)

    def unclaim(self, posted):
        self.sap.write("PATCH", ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
                       "(SupplierInvoice='%s',FiscalYear='%s')"
                       % (posted["SUPPLIERINVOICE"], posted["FISCALYEAR"]),
                       json.dumps({"PaymentRunID": "", "PaymentRunDate": None}),
                       "application/json")

    def test_no_run_has_one_unless_it_is_given_one(self):
        self.assertIsNone(PaymentRun(SAP, BANK, ACME, MODE).register)

    def test_it_holds_an_item_whose_claim_was_taken_off_in_sap(self):
        posted = self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        first = self.payments.run(self.monday, "R1")
        self.unclaim(posted)
        [held] = self.payments.run(self.monday, "R2").items
        self.assertEqual(held.status, "skipped")
        self.assertIn(first.msg_id, held.reason)
        # And a run without it pays again, which is what taking a claim off does.
        self.assertEqual([i.status for i in
                          PaymentRun(SAP, BANK, ACME, MODE).run(self.monday, "R3").items],
                         ["accepted"])

    def test_once_sap_has_cleared_it_the_register_lets_go(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.pay_on_monday()
        [item] = run.items
        self.assertIsNotNone(self.register.holder(item.document),
                             "accepted is not paid: still held until the statement")
        self.payments.reconcile(run)
        self.assertEqual(item.status, "cleared")
        self.assertIsNone(self.register.holder(item.document))

    def test_a_statement_posted_by_another_run_lets_go_of_it_too(self):
        """Released by SAP's document number, not by whose items a run holds."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.pay_on_monday()
        [item] = run.items
        somebody_else = Run(self.monday + datetime.timedelta(days=1), "R9")
        self.payments.reconcile(somebody_else)
        self.assertNotEqual(self.clearing("GLX-4711"), "")
        self.assertIsNone(self.register.holder(item.document))

    def test_a_payment_the_bank_refused_is_let_go(self):
        self.sap.invoice(INITECH, "INI-2026-17", "595.00")
        self.advance(self.monday)
        [refused] = self.payments.run(self.monday, "R1").items
        self.assertEqual(refused.status, "rejected")
        self.assertIsNone(self.register.holder(refused.document))

    def test_a_statement_posted_without_it_leaves_an_entry_that_outlives_the_claim(self):
        """What a second record costs, stated: `Register` says so too."""
        control(BANK, "PATCH", "/_mock/accounts/ACME", {
            "behaviour": "return-later",
            "parameters": {"end_to_end_id": "GLX-4711", "days": 3, "reason": CLOSED}})
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.pay_on_monday()
        friday = self.monday + datetime.timedelta(days=4)
        self.advance(friday)
        # Somebody without the register posts the statements: cleared, then back.
        PaymentRun(SAP, BANK, ACME, MODE).reconcile(run)
        self.assertEqual(run.items[0].status, "returned")
        self.assertEqual(self.cube_item("GLX-4711")["PaymentRunID"], "")
        # SAP says it is free; the entry nobody let go of says it is not.
        [stuck] = self.payments.run(friday, "R2").items
        self.assertEqual(stuck.status, "skipped")
        self.assertIn(run.msg_id, stuck.reason)


class TheRegisterOnDisk(StatementCase):
    """A register given a path is there for a payment program started again.

    SAP's claim would hold these items by itself, so each test that is about
    the file takes the claim off in SAP first, and what is left is the file.
    """

    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "in-payment.json")

    def test_a_new_process_with_the_same_file_does_not_pay_again(self):
        posted = self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        before = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        first = PaymentRun(SAP, BANK, ACME, MODE, register=Register(self.path))
        self.assertEqual([i.status for i in first.run(self.monday, "R1").items], ["accepted"])
        self.sap.write("PATCH", ODATA + "/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
                       "(SupplierInvoice='%s',FiscalYear='%s')"
                       % (posted["SUPPLIERINVOICE"], posted["FISCALYEAR"]),
                       json.dumps({"PaymentRunID": "", "PaymentRunDate": None}),
                       "application/json")
        # Nothing shared with the first but the file.
        second = PaymentRun(SAP, BANK, ACME, MODE, register=Register(self.path))
        self.assertEqual([i.status for i in second.run(self.monday, "R2").items], ["skipped"])
        self.advance(self.monday + datetime.timedelta(days=1))
        self.assertEqual(before - control(BANK, "GET", "/_mock/accounts/ACME")["balance"],
                         119000)

    def test_the_entry_is_on_disk_before_the_file_reaches_the_bank(self):
        """Write-ahead: a crash after sending must not find the register empty."""
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        runner = PaymentRun(SAP, BANK, ACME, MODE, register=Register(self.path))
        seen = []

        def sending(base, method, path, body=None, content_type=""):
            if path == "/payments":
                seen.append(dict(Register(self.path).entries))
            return call(base, method, path, body, content_type)

        with unittest.mock.patch.object(payment_run_module, "call", sending):
            run = runner.run(self.monday, "R1")
        [item] = run.items
        self.assertEqual(seen, [{item.document: {
            "msg_id": run.msg_id, "run_on": self.monday.isoformat(),
            "reference": "GLX-4711", "amount": "1190.00", "currency": CURRENCY}}])

    def test_cleared_and_refused_are_gone_from_the_file_as_well(self):
        self.post_three()
        runner = PaymentRun(SAP, BANK, ACME, MODE, register=Register(self.path))
        self.advance(self.monday)
        run = runner.run(self.monday, "R1")
        held = {e["reference"] for e in Register(self.path).entries.values()}
        self.assertEqual(held, {"GLX-4711", "UMB-0815"}, "INITECH's was refused")
        self.advance(self.monday + datetime.timedelta(days=1))
        runner.reconcile(run)
        self.assertEqual(Register(self.path).entries, {})


class TheRegisterItself(unittest.TestCase):
    """What `Register` does with a file, with no mock running."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "in-payment.json")
        self.run_ = Run(datetime.date(2026, 10, 5), "R1")
        self.item = Item(document="1010/2026/1900000001", supplier="1000013",
                         reference="INV-1", amount="10.00")

    def test_a_file_that_is_not_there_yet_is_an_empty_register(self):
        self.assertEqual(Register(self.path).entries, {})
        self.assertFalse(os.path.exists(self.path), "reading writes nothing")

    def test_it_lets_go_by_the_full_key_or_by_saps_document_number(self):
        for key in ("1010/2026/1900000001", "1900000001"):
            with self.subTest(key=key):
                register = Register(self.path)
                register.hold([self.item], self.run_)
                self.assertTrue(register.release(key))
                self.assertEqual(Register(self.path).entries, {})

    def test_a_number_that_only_ends_the_same_is_not_that_document(self):
        register = Register(self.path)
        register.hold([self.item], self.run_)
        self.assertFalse(register.release("900000001"))
        self.assertFalse(register.release("1010/2026/190000000"))
        self.assertIsNotNone(register.holder(self.item.document))

    def test_no_half_written_file_is_left_beside_it(self):
        Register(self.path).hold([self.item], self.run_)
        self.assertEqual(os.listdir(self.directory.name), ["in-payment.json"])

    def test_a_write_that_dies_half_way_leaves_the_register_that_was_there(self):
        register = Register(self.path)
        register.hold([self.item], self.run_)
        other = Item(document="1010/2026/1900000002", supplier="1000016",
                     reference="INV-2", amount="20.00")
        with unittest.mock.patch.object(payment_run_module.os, "fsync",
                                        side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                register.hold([other], self.run_)
        # The file is still the one from before, whole and readable.
        self.assertEqual(list(Register(self.path).entries), [self.item.document])

    def test_a_file_from_some_other_version_is_refused_not_guessed_at(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 2, "in_payment": {}}, handle)
        with self.assertRaises(ValueError) as refused:
            Register(self.path)
        self.assertIn("version is 2", str(refused.exception))


class WhenSomethingAnswersBadly(MocksCase):
    """mock-bank#73's notes: say what went wrong rather than stop or stay silent."""

    def test_sap_refusing_a_statement_is_recorded_against_it(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.payments.run(self.today, "R1")
        control(BANK, "POST", "/_mock/advance?to=%s"
                % (self.today + datetime.timedelta(days=1)).isoformat())
        control(SAP, "POST", "/_mock/faults", {"method": "POST", "match": "/sap/bc/idoc",
                                               "status": 503, "count": 1})
        self.payments.reconcile(run)
        self.assertIn("503", run.statements[0]["error"])
        self.assertEqual(run.problems, [run.statements[0]["error"]])

    def test_a_bank_that_answers_with_an_error_is_a_problem_not_silence(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        nowhere = PaymentRun(SAP, SAP, ACME, MODE)           # SAP is no bank: 404 throughout
        run = nowhere.run(self.today, "R1")
        nowhere.reconcile(run)
        self.assertEqual(len(run.problems), 3, run.problems)
        self.assertIn("answered 404 to the payment file", run.problems[0])
        self.assertIn("no acknowledgement was read" if NACHA
                      else "no status report was read", run.problems[1])
        self.assertIn("no statement was read", run.problems[2])
        self.assertEqual(run.items[0].status, "sent")

    def test_a_bank_that_does_not_answer_is_a_problem_not_silence(self):
        # A port nothing listens on: no HTTP status at all, which used to be an
        # exception that stopped the run half way (mock-bank#88).
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        down = PaymentRun(SAP, closed_port(), ACME, MODE)
        run = down.run(self.today, "R1")
        down.reconcile(run)
        self.assertEqual(len(run.problems), 3, run.problems)
        self.assertIn("the bank did not answer, so the payment file was not sent",
                      run.problems[0])
        self.assertIn("mailbox did not answer", run.problems[1])
        self.assertIn("mailbox did not answer", run.problems[2])
        # The file never reached the bank: the item is as it was, to send again.
        self.assertEqual([i.status for i in run.items], ["selected"])

    def test_sap_not_answering_the_selection_selects_nothing_and_says_so(self):
        run = PaymentRun(closed_port(), BANK, ACME, MODE).run(self.today, "R1")
        self.assertEqual(run.items, [])
        self.assertEqual(len(run.problems), 1, run.problems)
        self.assertIn("SAP did not answer, so no open item was selected", run.problems[0])

    def test_sap_not_answering_a_statement_is_recorded_against_it(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        run = self.payments.run(self.today, "R1")
        control(BANK, "POST", "/_mock/advance?to=%s"
                % (self.today + datetime.timedelta(days=1)).isoformat())
        self.payments.session = SapSession(closed_port())
        self.payments.reconcile(run)
        self.assertIn("SAP did not answer, so statement", run.statements[0]["error"])
        self.assertEqual(run.problems, [run.statements[0]["error"]])

    def test_two_payments_of_the_missing_amount_are_both_named(self):
        run = Run(self.today, "R1", [
            Item("1/2026/1", GLOBEX, "GLX-1", "238.00", status="accepted"),
            Item("1/2026/2", UMBRELLA, "UMB-1", "238.00", status="accepted"),
            Item("1/2026/3", UMBRELLA, "UMB-2", "50.00", status="accepted")])
        self.payments.name_the_shortfall(run, "7", "2026-10-05", Decimal("238"))
        self.assertEqual([i.status for i in run.items],
                         ["unreconciled", "unreconciled", "accepted"])
        self.assertIn("GLX-1, UMB-1", run.items[0].reason)
        self.assertIn("cannot say which", run.items[0].reason)


class TheNachaFileHeader(unittest.TestCase):
    """What tells one run's NACHA file from another's. Needs neither mock."""

    def test_every_identification_it_takes_has_its_own_time_and_modifier(self):
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        identifications = [a + b + c for a in alphabet for b in [""] + list(alphabet)
                           for c in ([""] if not b else [""] + list(alphabet))]
        pairs = {nacha_time_and_modifier(i) for i in identifications}
        self.assertEqual(len(pairs), len(identifications))
        self.assertTrue(all(int(time[:2]) < 24 and int(time[2:]) < 60
                            for time, _modifier in pairs))

    def test_one_it_cannot_tell_apart_is_refused_before_anything_is_selected(self):
        for identification in ("RUN1", "r1", "R-1", ""):
            run = PaymentRun(closed_port(), closed_port(), ACME, "nacha").run(
                datetime.date(2026, 10, 2), identification)
            self.assertEqual(run.items, [])
            self.assertEqual(len(run.problems), 1, run.problems)
            self.assertIn("so no open item was selected", run.problems[0])

    def test_a_company_identification_over_ten_characters_is_refused(self):
        run = PaymentRun(closed_port(), closed_port(), dict(ACME, company_id="0" * 11),
                         "nacha").run(datetime.date(2026, 10, 2), "R1")
        self.assertIn("company identification", run.problems[0])

    def test_a_control_character_in_the_company_is_refused(self):
        # ASCII, so `str.isascii()` passed it, and no character a record holds.
        for company, named in ((dict(ACME, name="ACME\nCORP"), "company name"),
                               (dict(ACME, company_id="12345\t6789"),
                                "company identification")):
            run = PaymentRun(closed_port(), closed_port(), company, "nacha").run(
                datetime.date(2026, 10, 2), "R1")
            self.assertEqual(run.items, [])
            self.assertIn(named, run.problems[0])
            self.assertIn("printable ASCII", run.problems[0])
            self.assertIn("it holds", run.problems[0])

    def test_a_company_routing_number_that_is_not_nine_digits_is_refused(self):
        without = {key: value for key, value in ACME.items() if key != "routing"}
        for company in ([dict(ACME, routing=r) for r in
                         ("", "99999999", "9999999921", "99999999X", "99999999\u00b2")]
                        + [without]):
            with self.subTest(routing=company.get("routing")):
                run = PaymentRun(closed_port(), closed_port(), company, "nacha").run(
                    datetime.date(2026, 10, 2), "R1")
                self.assertEqual(run.items, [])
                self.assertEqual(len(run.problems), 1, run.problems)
                self.assertIn("company routing number", run.problems[0])
                self.assertIn("so no open item was selected", run.problems[0])

    def test_every_printable_character_is_one_a_record_holds(self):
        printable = "".join(chr(code) for code in range(0x20, 0x7F))
        self.assertEqual(payment_run_module.unprintable(printable), [])
        self.assertEqual(payment_run_module.unprintable("a\x1f\x7f\u00e9\x1f"),
                         ["\x1f", "\x7f", "\u00e9"])

    def test_a_bare_status_line_is_read_as_no_status(self):
        run = Run(datetime.date(2026, 10, 2), "R1", [
            Item("1/2026/1", GLOBEX, "GLX-1", "10.00", status="sent")],
            nacha_origin=ACME["company_id"])
        ack = "ACKNOWLEDGEMENT A1\nFILE %s\nSTATUS\n" % run.msg_id
        # `patch.object`, not `patch("payment_run.call")`: the module is
        # `examples.payment_run` here and `mockbank.examples.payment_run` out of
        # the wheel (mock-bank#169), and a patch target written as a string would have to
        # name one of them. The module object is the same either way.
        with unittest.mock.patch.object(payment_run_module, "call",
                                        return_value=(200, ack.encode())):
            PaymentRun(SAP, BANK, ACME, "nacha").read_status(run)
        self.assertEqual((run.items[0].status, run.duplicate), ("sent", False))

    def test_an_unknown_file_format_is_an_error(self):
        with self.assertRaises(ValueError):
            PaymentRun(SAP, BANK, ACME, "NACHA")


class TwoSuppliersWithOneInvoiceNumber(unittest.TestCase):
    """mock-bank#171, mock-bank#164 item 5: an invoice number is a supplier's own sequence.

    Two suppliers can both bill `INV-1`, so a reference does not identify an item.
    SAP's accounting document does, and SAP returns it on every CLEARED and
    REOPENED row. Keyed on the reference, a dict of items collapses the two into
    one *before any matching happens* and the earlier one is simply gone - so a
    clearing can be recorded against an item SAP never cleared, while the item it
    did clear is left looking unpaid and is paid again by the next run.

    No mock has to be running: SAP's answer is the thing under test, so it is
    handed over directly at `post_idoc`, which is where the run reads it. That
    also makes the two-supplier case deterministic, which it would not be against
    a live SAP picking one of two matching items itself (mock-sap#87).
    """

    GLOBEX_DOCUMENT = "0100000007"
    UMBRELLA_DOCUMENT = "0100000008"

    def both(self):
        globex = Item(document="1000/2026/%s" % self.GLOBEX_DOCUMENT, supplier=GLOBEX,
                      reference="INV-1", amount="10.00", status="accepted")
        umbrella = Item(document="1000/2026/%s" % self.UMBRELLA_DOCUMENT,
                        supplier=UMBRELLA, reference="INV-1", amount="20.00",
                        status="accepted")
        return Run(datetime.date(2026, 10, 2), "R1", [globex, umbrella]), globex, umbrella

    def post(self, run, applied):
        """One statement posted, with SAP's answer to it supplied."""
        statement = {"number": "1", "day": "2026-10-05", "currency": "EUR",
                     "opening": Decimal("100.00"), "closing": Decimal("70.00"),
                     "lines": [{"end_to_end_id": "INV-1", "amount": Decimal("10.00"),
                                "side": "DBIT", "returned_for": ""},
                               {"end_to_end_id": "INV-1", "amount": Decimal("20.00"),
                                "side": "DBIT", "returned_for": ""}]}
        runner = PaymentRun(SAP, BANK, ACME, MODE)
        with unittest.mock.patch.object(runner.session, "post_idoc", return_value=applied):
            return runner.post_statement(run, statement)

    def test_each_clearing_goes_to_the_item_sap_says_it_cleared(self):
        run, globex, umbrella = self.both()
        self.post(run, {"CLEARED": [
            {"LINE": 1, "REFERENCE": "INV-1",
             "ACCOUNTINGDOCUMENT": self.GLOBEX_DOCUMENT,
             "CLEARINGDOCUMENT": "0200000001", "AMOUNT": "10.00"},
            {"LINE": 2, "REFERENCE": "INV-1",
             "ACCOUNTINGDOCUMENT": self.UMBRELLA_DOCUMENT,
             "CLEARINGDOCUMENT": "0200000002", "AMOUNT": "20.00"}]})
        # Keyed on the reference, both rows resolve to whichever item the dict
        # comprehension kept, the first clearing is written to it, and the second
        # is then dropped because that item is no longer `accepted`.
        self.assertEqual((globex.status, globex.reason), ("cleared", "0200000001"))
        self.assertEqual((umbrella.status, umbrella.reason), ("cleared", "0200000002"))

    def test_a_row_naming_no_accounting_document_is_said_out_loud(self):
        run, globex, umbrella = self.both()
        self.post(run, {"CLEARED": [{"LINE": 1, "REFERENCE": "INV-1",
                                     "CLEARINGDOCUMENT": "0200000001",
                                     "AMOUNT": "10.00"}]})
        self.assertEqual((globex.status, umbrella.status), ("accepted", "accepted"))
        self.assertTrue(any("names no accounting document" in p for p in run.problems),
                        run.problems)


if __name__ == "__main__":
    unittest.main()
