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
import unittest
import unittest.mock
import urllib.parse
import urllib.request
from decimal import Decimal

from mockacme import payment_run as payment_run_module
from mockacme.bank_messages import call
from mockacme.payment_run import (ITEMS, ODATA, OPEN_SUPPLIER_ITEMS, Item, PaymentRun, Run,
                         SapSession, nacha_time_and_modifier, odata)

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
        # And said out loud, because the reference was withheld from SAP.
        said = [p for p in run.problems if "GLX-4711" in p]
        self.assertEqual(len(said), 1, run.problems)
        self.assertIn("money arriving", said[0])
        self.assertIn("1190.00", said[0])

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
        self.assertTrue(items["UMB-0815"].reason.startswith(CLOSED), items["UMB-0815"].reason)
        self.assertEqual([i.reference for i in self.payments.select(self.bank_today())],
                         ["UMB-0815"])


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


class ASecondRunBeforeTheStatement(StatementCase):
    """#2, the way that is not fixed: this test states what is wrong today.

    Nothing clears an open item until the statement is posted, and SAP has no
    state between open and cleared that a run could write (mock-sap#90). So a
    second run started before the statement arrives selects the invoice again,
    under a new message identification the bank has never seen, and the bank
    pays it again. When that is fixed this test fails, and is rewritten to say
    the invoice is paid once.
    """

    def test_the_invoice_is_paid_twice(self):
        self.sap.invoice(GLOBEX, "GLX-4711", "1190.00")
        self.advance(self.monday)
        before = control(BANK, "GET", "/_mock/accounts/ACME")["balance"]
        first = self.payments.run(self.monday, "R1")
        second = self.payments.run(self.monday, "R2")
        self.assertEqual([(r.duplicate, [i.status for i in r.items]) for r in (first, second)],
                         [(False, ["accepted"]), (False, ["accepted"])])
        self.advance(self.monday + datetime.timedelta(days=1))
        self.assertEqual(before - control(BANK, "GET", "/_mock/accounts/ACME")["balance"],
                         2 * 119000)


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
