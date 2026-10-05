"""Integration tests for invoice_check, against mock-sap and mock-edi.

    python3 -m unittest -v tests.test_invoice_check

`tests/__init__.py` starts the mocks, and says how to point the tests at
mocks that are already running.
"""
import datetime
import json
import os
import re
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

import tests
from mockacme import po_bridge
from mockacme.invoice_check import (PO_SERVICE, InvoiceCheck, Sap, invoic_idoc, read_810,
                                    send_order)
from mockacme.procure_to_pay import DurableInvoiceCheck

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")


def sap_date(raw):
    """OData V2's `/Date(1788220800000)/`, as a date."""
    found = re.search(r"-?\d+", raw or "")
    return None if not found else (
        datetime.datetime(1970, 1, 1)
        + datetime.timedelta(milliseconds=int(found.group()))).date()


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class SupplierInvoices(unittest.TestCase):

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.check = InvoiceCheck(self.sap, EDI, our_id="ACME")

    def order(self, widget_price="12.50", currency="USD"):
        """A PO in SAP for 100 widgets and 40 brackets, sent to the supplier."""
        po = self.sap.request("POST", PO_SERVICE + "/A_PurchaseOrder", {
            "PurchaseOrderType": "NB", "CompanyCode": "1710",
            "PurchasingOrganization": "1710", "PurchasingGroup": "001",
            "Supplier": "1000012", "DocumentCurrency": currency,
            "to_PurchaseOrderItem": [
                {"Material": "TG11", "OrderQuantity": "100", "NetPriceAmount": widget_price,
                 "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"},
                {"Material": "TG12", "OrderQuantity": "40", "NetPriceAmount": "4.15",
                 "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"},
            ]})["d"]["PurchaseOrder"]
        summary = send_order(self.sap, EDI, po, sender="ACME")
        self.assertTrue(summary["accepted"])
        return po

    def supplier_behaves(self, behaviour):
        control(EDI, "PATCH", "/_mock/partners/ACME", {"behaviour": behaviour})

    def invoice_idocs(self):
        return control(SAP, "GET", "/_mock/idocs?mestyp=INVOIC")["results"]

    def supplier_invoices(self, reference):
        q = urllib.parse.urlencode({
            "$filter": "SupplierInvoiceIDByInvcgParty eq '%s'" % reference,
            "$format": "json"})
        return self.sap.request(
            "GET", "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                   "/A_SupplierInvoice?" + q)["d"]["results"]

    def invoice_items(self, supplier_invoice):
        q = urllib.parse.urlencode({
            "$filter": "SupplierInvoice eq '%s'" % supplier_invoice,
            "$format": "json"})
        return self.sap.request(
            "GET", "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"
                   "/A_SuplrInvcItemPurOrdRef?" + q)["d"]["results"]

    def open_items(self):
        """What a payment run would select: open supplier items."""
        q = urllib.parse.urlencode({
            "$filter": "AccountingDocumentItemType eq 'K' and "
                       "ClearingAccountingDocument eq ''",
            "$format": "json"})
        return self.sap.request(
            "GET", "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
                   "/A_OperationalAcctgDocItemCube?" + q)["d"]["results"]

    def test_matching_invoice_is_posted(self):
        po = self.order()

        [result] = self.check.run()
        self.assertEqual((result["po"], result["status"], result["problems"]),
                         (po, "posted", []))
        [idoc] = self.invoice_idocs()
        self.assertEqual((idoc["docnum"], idoc["status"]), (result["idoc"], "53"))

    def test_a_posted_invoice_leaves_money_owed(self):
        """A posted invoice has to leave money owed, or nothing was posted.

        Every test here asserted what SAP *received* - the IDoc and its status -
        and none asserted what posting it created. The IDoc never named the
        supplier, so SAP created no supplier invoice and no payable and answered
        status 53 anyway, and five green tests said it was fine. It went
        unnoticed until there were payables to miss: 0.12.0 added them, and the
        first run of all three mocks together found this the next day. A payment
        run had nothing to pay - the invoice was approved, posted, and owed to
        nobody (mock-sap#67, mock-sap#68).
        """
        po = self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")
        self.assertTrue(result["supplier_invoice"], "posting it created nothing")

        invoices = self.supplier_invoices(result["invoice"])
        self.assertEqual(len(invoices), 1, invoices)
        self.assertEqual(invoices[0]["SupplierInvoiceIDByInvcgParty"],
                         result["invoice"],
                         "a payment run matches the bank's answer on this")
        self.assertEqual(invoices[0]["InvoicingParty"], "1000012")

        items = self.open_items()
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]["Supplier"], "1000012")
        # 100 widgets at 12.50 and 40 brackets at 4.15, as the 810 billed.
        self.assertEqual(abs(Decimal(items[0]["AmountInTransactionCurrency"])),
                         Decimal("1416.00"))
        self.assertEqual(items[0]["TransactionCurrency"], "USD")

    def test_the_payable_lines_carry_the_amounts_and_the_order(self):
        """A payable's lines carry the amounts, not just its total.

        A total that is right over lines worth nothing is still wrong: matching
        in SAP happens per item, against the purchase order line. NETWR and
        VGBEL/VGPOS on each E1EDP01 are what put them there.
        """
        po = self.order()

        [result] = self.check.run()
        items = self.invoice_items(result["supplier_invoice"])

        self.assertEqual(len(items), 2, items)
        by_amount = sorted(Decimal(i["SupplierInvoiceItemAmount"]) for i in items)
        # 100 widgets at 12.50, and 40 brackets at 4.15.
        self.assertEqual(by_amount, [Decimal("166.00"), Decimal("1250.00")])
        self.assertEqual({i["PurchaseOrder"] for i in items}, {po})

    def test_the_payable_falls_due_on_the_invoices_own_terms(self):
        """Net 30 reaches the payable, so a payment run picks it up when due.

        The 810 carries its net days in ITD07 (`ITD*08*3*2**10**30`) and its
        date in BIG01. Posting with neither left the payable due immediately,
        which is a different invoice from the one the supplier sent.

        What this cannot show is that the baseline is the *invoice's* date
        rather than the posting date: mock-edi bills on the current date, so the
        two are the same here and an assertion would prove nothing. The date is
        read from BIG01 and sent as E1EDK03/026 - see invoic_idoc - and a test
        that could tell them apart needs a supplier that backdates.
        """
        po = self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")

        [item] = self.open_items()
        [invoice] = self.supplier_invoices(result["invoice"])
        baseline = sap_date(invoice["DueCalculationBaseDate"])
        self.assertEqual(sap_date(item["NetDueDate"]),
                         baseline + datetime.timedelta(days=30))
        self.assertEqual(invoice["PaymentTerms"], "NT30")

    def test_short_shipment_billed_as_shipped_is_posted(self):
        # Checking the invoice against the PO quantity would block this one.
        # The supplier shipped 80 and 32 and billed 80 and 32: that is correct.
        self.supplier_behaves("short-ship")
        self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")

    def test_an_invoice_in_the_wrong_currency_is_blocked(self):
        """The same number in another currency is not the same price.

        Every other check subtracts and compares bare decimals, so a USD invoice
        against a EUR order passes all of them: the figures agree and mean
        different things. This drives `problems` directly rather than arranging a
        supplier that misbills, because the check is the thing under test and an
        810 in the wrong currency is not something mock-edi will send once the
        850 has declared one.
        """
        po_number = self.order(currency="EUR")
        po = self.sap.purchase_order(po_number)
        invoice = {"number": "INV-FX", "po": po_number, "currency": "USD",
                   "lines": {"00010": (Decimal("100"), Decimal("12.50"))},
                   "total": Decimal("1250.00")}

        problems = self.check.problems(invoice, po)

        self.assertTrue(any("is in USD" in p and "is in EUR" in p for p in problems),
                        problems)

    def test_an_invoice_that_names_no_currency_is_not_blocked_for_it(self):
        """Absence is not disagreement.

        CUR is optional in an 810, and a partner that omits it is not telling us
        the price is in the wrong money. Blocking for a missing segment would
        reject invoices that are fine, so the check needs both sides before it
        says anything - which is the guard this asserts.
        """
        po_number = self.order(currency="EUR")
        po = self.sap.purchase_order(po_number)
        invoice = {"number": "INV-NOCUR", "po": po_number, "currency": "",
                   "lines": {"00010": (Decimal("100"), Decimal("12.50")),
                             "00020": (Decimal("40"), Decimal("4.15"))},
                   "total": Decimal("1416.00")}

        problems = self.check.problems(invoice, po)

        # Not "no problems at all": nothing has read an 856 here, so the
        # quantity check speaks up. The claim is narrower and is the one that
        # matters - a missing CUR is not itself a reason to block.
        self.assertEqual([p for p in problems if "is in" in p], [], problems)

    def test_the_order_declares_its_currency_so_the_supplier_bills_it(self):
        """A EUR order comes back invoiced in EUR, and so is payable by SEPA.

        Without the 850's CUR segment mock-edi bills its own default, USD, and a
        payment run refuses the item because a SEPA transfer is in EUR. This is
        the test that a purchase order's currency survives the round trip.
        """
        self.order(currency="EUR")

        [result] = self.check.run()

        self.assertEqual((result["status"], result["problems"]), ("posted", []))
        [item] = self.open_items()
        self.assertEqual(item["TransactionCurrency"], "EUR")

    def test_price_disagreement_is_blocked(self):
        # We ordered widgets at 11.00; the supplier's catalogue says 12.50,
        # and a real supplier bills its own price.
        self.order(widget_price="11.00")

        [result] = self.check.run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["problems"], ["item 00010 billed at 12.50, ordered at 11.00"])
        self.assertEqual(self.invoice_idocs(), [])

    def test_an_idoc_sap_would_not_post_is_not_treated_as_posted(self):
        # SAP answers 201 with a docnum and then declines to post the invoice.
        # Reading the docnum and stopping there books an invoice SAP rejected.
        control(SAP, "POST", "/_mock/idoc-posting",
                {"mestyp": "INVOIC", "status": "51",
                 "message": "Posting period 08 2026 is not open"})
        self.order()

        [result] = self.check.run()
        self.assertEqual(result["status"], "not posted")
        self.assertIn("status 51", result["problems"][0])

        # SAP's own record agrees, and the invoice is not marked as posted here,
        # so the resend after someone opens the period is not a duplicate
        [idoc] = self.invoice_idocs()
        self.assertEqual(idoc["status"], "51")
        self.assertEqual(self.check.posted, set())

        control(SAP, "DELETE", "/_mock/idoc-posting")

    def test_duplicate_invoice_is_posted_once(self):
        # A supplier with a retry bug sends the same invoice again a second
        # later.  Release it now rather than sleeping.
        self.supplier_behaves("duplicate-invoice")
        self.order()

        [first] = self.check.run()
        control(EDI, "POST", "/_mock/advance?all")
        [second] = self.check.run()

        self.assertEqual(first["invoice"], second["invoice"])
        self.assertEqual(first["status"], "posted")
        self.assertEqual(second["status"], "blocked")
        self.assertIn("already been posted", second["problems"][0])
        self.assertEqual(len(self.invoice_idocs()), 1)


def one_line_order(sap, quantity, price="12.50"):
    """A PO in SAP for one line of widgets. Returns its number."""
    return sap.request("POST", PO_SERVICE + "/A_PurchaseOrder", {
        "PurchaseOrderType": "NB", "CompanyCode": "1710",
        "PurchasingOrganization": "1710", "PurchasingGroup": "001",
        "Supplier": "1000012", "DocumentCurrency": "USD",
        "to_PurchaseOrderItem": [
            {"Material": "TG11", "OrderQuantity": quantity, "NetPriceAmount": price,
             "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"}]})["d"]["PurchaseOrder"]


def lines_of(document, *tags):
    """The segments of an X12 payload that start with one of `tags`."""
    return [segment for segment in document["payload"].replace("\n", "").split("~")
            if segment.split("*")[0] in tags]


class AFractionalQuantity(unittest.TestCase):
    """#8: an order for 2.5 goes onto the wire as 2.5.

    It went as 2. The supplier confirmed 2, shipped 2 and billed 2, every
    document agreed with every other, and the invoice was posted with no
    problem found - for less than was ordered.
    """

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)

    def what_the_supplier_made_of_it(self):
        """The supplier's own documents, looked at without collecting them."""
        return {d["code"]: d for d in control(EDI, "GET", "/_mock/mailbox?partner=ACME&leave")}

    def test_sap_holds_the_fraction_this_starts_from(self):
        po = one_line_order(self.sap, "2.5")
        [item] = self.sap.purchase_order(po)["to_PurchaseOrderItem"]["results"]
        self.assertEqual(Decimal(item["OrderQuantity"]), Decimal("2.5"))

    def test_the_supplier_is_asked_for_and_bills_what_was_ordered(self):
        po = one_line_order(self.sap, "2.5")
        self.assertTrue(send_order(self.sap, EDI, po, sender="ACME")["accepted"])
        documents = self.what_the_supplier_made_of_it()
        # The 855 echoes the PO1 it read: what arrived, in the supplier's words.
        self.assertEqual([l.split("*")[2] for l in lines_of(documents["855"], "PO1")], ["2.5"])
        self.assertEqual([l.split("*")[2] for l in lines_of(documents["810"], "IT1")], ["2.5"])
        self.assertEqual(lines_of(documents["810"], "TDS"), ["TDS*3125"])

        [result] = InvoiceCheck(self.sap, EDI, our_id="ACME").run()
        self.assertEqual((result["status"], result["problems"]), ("posted", []))

    def test_the_bridge_sends_the_fraction_as_well(self):
        po = one_line_order(self.sap, "0.125")
        bridge = po_bridge.Bridge(po_bridge.Sap(SAP), EDI, our_id="ACME",
                                  supplier_id="MOCKEDI")
        self.assertTrue(bridge.send(po)["accepted"])
        documents = self.what_the_supplier_made_of_it()
        self.assertEqual([l.split("*")[2] for l in lines_of(documents["855"], "PO1")],
                         ["0.125"])

    def test_a_whole_number_is_written_without_a_point_or_an_exponent(self):
        for raw, written in (("100.000", "100"), ("100", "100"), ("2.500", "2.5"),
                             ("0.125", "0.125"), ("1000000.000", "1000000"),
                             ("1E+2", "100"), (40, "40"), ("0.000", "0")):
            with self.subTest(raw=raw):
                self.assertEqual(po_bridge.x12_quantity(raw), written)


class OneMailboxTwoReaders(unittest.TestCase):
    """#8: the bridge and the invoice check each take only what they read.

    Both collected the supplier's whole mailbox, and collecting takes a document
    out of it. Whichever ran first took the other's documents and dropped them.
    """

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.bridge = po_bridge.Bridge(po_bridge.Sap(SAP), EDI, our_id="ACME",
                                       supplier_id="MOCKEDI")
        self.check = InvoiceCheck(self.sap, EDI, our_id="ACME")
        self.po = one_line_order(self.sap, "100")
        self.assertTrue(self.bridge.send(self.po)["accepted"])

    def test_the_bridge_first_leaves_the_invoice_to_be_checked(self):
        self.assertEqual(list(self.bridge.receive()), [self.po])
        [result] = self.check.run()
        self.assertEqual((result["po"], result["status"]), (self.po, "posted"))

    def test_the_check_first_leaves_the_confirmation_to_be_posted(self):
        [result] = self.check.run()
        self.assertEqual(result["status"], "posted")
        confirmed = self.bridge.receive()
        self.assertEqual(list(confirmed), [self.po])
        self.assertEqual(confirmed[self.po]["exceptions"], [])

    def test_what_neither_reads_is_still_in_the_mailbox(self):
        """The 997 is nobody's here, and taking it would be throwing it away."""
        self.bridge.receive()
        self.check.run()
        left = control(EDI, "GET", "/_mock/mailbox?partner=ACME&leave")
        self.assertEqual([d["code"] for d in left], ["997"])


class ASupplierThatChargesTax(unittest.TestCase):
    """#8: mock-edi started with `--tax-rate`, which made every invoice blocked.

    The 810's total includes the tax and its lines do not, so "lines add up to
    X, invoice total is Y" was the answer to a correct invoice. The mock is
    started here because the rate is a start-up option.
    """

    @classmethod
    def setUpClass(cls):
        cls.taxing = tests.another("mockedi", ["--tax-rate", "0.0825"])

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(self.taxing, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.po = one_line_order(self.sap, "100")
        self.assertTrue(send_order(self.sap, self.taxing, self.po, sender="ACME")["accepted"])

    def test_the_supplier_really_does_charge_it(self):
        """Or the test below passes against a mock that ignored the option."""
        [invoice] = [d for d in control(self.taxing, "GET", "/_mock/mailbox?partner=ACME&leave")
                     if d["code"] == "810"]
        self.assertEqual(lines_of(invoice, "TDS", "TXI"), ["TDS*135313", "TXI*ST*103.13"])

    def test_a_correct_taxed_invoice_is_posted_and_owed_in_full(self):
        [result] = InvoiceCheck(self.sap, self.taxing, our_id="ACME").run()
        self.assertEqual((result["status"], result["problems"]), ("posted", []))
        q = urllib.parse.urlencode({
            "$filter": "AccountingDocument eq '%s'" % result["accounting_document"],
            "$format": "json"})
        rows = self.sap.request(
            "GET", "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV"
                   "/A_OperationalAcctgDocItemCube?" + q)["d"]["results"]
        booked = sorted((row["AccountingDocumentItemType"], row.get("GLAccount") or "",
                         abs(Decimal(row["AmountInTransactionCurrency"])))
                        for row in rows)
        # Owed gross; the expense and the input tax booked apart, not the tax
        # expensed with the goods.
        self.assertEqual(booked, [("K", "", Decimal("1353.13")),
                                  ("S", "0000154000", Decimal("103.13")),
                                  ("S", "0000400000", Decimal("1250.00"))])


class ReadingTaxFromAnInvoice(unittest.TestCase):
    """The 810 reader and the match on their own, with no mock involved."""

    INVOICE = ("ST*810*0001~BIG*20261005*INV-1**4500000001~CUR*SE*USD~"
               "IT1*00010*100*EA*12.50**VP*WIDGET-001~TDS*135313~%sCTT*1~SE*8*0001~")
    PO = {"Supplier": "1000012", "DocumentCurrency": "USD", "to_PurchaseOrderItem": {
        "results": [{"PurchaseOrderItem": "00010", "NetPriceAmount": "12.50",
                     "OrderQuantity": "100.000"}]}}

    def check(self, invoice):
        check = InvoiceCheck(None, "", "ACME")
        check.shipped = {"4500000001": {"00010": Decimal("100")}}
        return check.problems(invoice, self.PO)

    def test_an_invoice_without_tax_reads_as_no_tax(self):
        invoice = read_810(self.INVOICE.replace("135313", "125000") % "")
        self.assertEqual(invoice["tax"], Decimal("0.00"))
        self.assertEqual(self.check(invoice), [])
        self.assertNotIn("<SUMID>205</SUMID>", invoic_idoc(invoice, self.PO))
        self.assertNotIn("<SUMID>011</SUMID>", invoic_idoc(invoice, self.PO))

    def test_the_tax_is_added_in_before_the_total_is_compared(self):
        invoice = read_810(self.INVOICE % "TXI*ST*103.13~")
        self.assertEqual(invoice["tax"], Decimal("103.13"))
        self.assertEqual(self.check(invoice), [])

    def test_two_taxes_are_both_counted(self):
        invoice = read_810(self.INVOICE % "TXI*ST*100.00~TXI*CT*3.13~")
        self.assertEqual(invoice["tax"], Decimal("103.13"))
        self.assertEqual(self.check(invoice), [])

    def test_a_taxed_total_that_still_does_not_add_up_is_blocked_and_says_both(self):
        invoice = read_810(self.INVOICE % "TXI*ST*100.00~")
        self.assertEqual(self.check(invoice), [
            "lines add up to 1250.00 and tax to 100.00, invoice total is 1353.13"])

    def test_tax_that_is_not_on_the_invoice_is_not_assumed(self):
        """A total above its lines with no TXI is a wrong total, not hidden tax."""
        invoice = read_810(self.INVOICE % "")
        self.assertEqual(self.check(invoice), [
            "lines add up to 1250.00, invoice total is 1353.13"])

    def test_sap_is_told_the_net_the_tax_and_the_gross_apart(self):
        idoc = invoic_idoc(read_810(self.INVOICE % "TXI*ST*103.13~"), self.PO)
        for sumid, amount in (("010", "1353.13"), ("011", "1250.00"), ("205", "103.13")):
            self.assertIn("<SUMID>%s</SUMID><SUMME>%s</SUMME>" % (sumid, amount), idoc)



def an_810(number, po, quantity, price="12.50"):
    """A supplier's invoice for one line, written by hand."""
    total = (Decimal(quantity) * Decimal(price) * 100).to_integral_value()
    return ("ST*810*0001~BIG*20261005*%s**%s~CUR*SE*USD~IT1*00010*%s*EA*%s**VP*WIDGET-001~"
            "TDS*%d~CTT*1~SE*7*0001~" % (number, po, quantity, price, total))


class MoreThanWasOrdered(unittest.TestCase):
    """#12: what is billed is held to the order, not only to the ship notice.

    The 856 is the supplier's own account of what it sent. A supplier that
    ships 150 against an order for 100 and bills 150 agrees with itself, and
    that was all the match asked.
    """

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.po_number = one_line_order(self.sap, "100")
        self.po = self.sap.purchase_order(self.po_number)

    def problems(self, check, number, quantity, shipped="1000"):
        check.shipped = {self.po_number: {"00010": Decimal(shipped)}}
        return check.problems(read_810(an_810(number, self.po_number, quantity)), self.po)

    def test_shipped_and_billed_over_the_order_is_blocked(self):
        check = InvoiceCheck(self.sap, EDI, "ACME")
        self.assertEqual(self.problems(check, "INV-A", "150", shipped="150"),
                         ["item 00010 bills 150, ordered 100"])

    def test_exactly_the_order_is_not(self):
        check = InvoiceCheck(self.sap, EDI, "ACME")
        self.assertEqual(self.problems(check, "INV-A", "100"), [])

    def test_no_tolerance_is_assumed(self):
        """A real order item may allow some; mock-sap's says nothing, so none."""
        check = InvoiceCheck(self.sap, EDI, "ACME")
        self.assertEqual(self.problems(check, "INV-A", "100.5"),
                         ["item 00010 bills 100.5, ordered 100"])

    def test_a_second_invoice_is_counted_with_the_first(self):
        """Each consignment is billed apart, so one invoice alone proves little."""
        self.assertTrue(send_order(self.sap, EDI, self.po_number, sender="ACME")["accepted"])
        check = InvoiceCheck(self.sap, EDI, "ACME")
        [first] = check.run()
        self.assertEqual(first["status"], "posted")          # 100 of 100, from mock-edi
        self.assertEqual(check.already_billed(self.po_number, "00010"), Decimal("100"))
        self.assertEqual(self.problems(check, "INV-B", "10"),
                         ["item 00010 bills 10, with 100 already billed, ordered 100"])

    def test_an_invoice_that_was_blocked_is_not_counted(self):
        check = InvoiceCheck(self.sap, EDI, "ACME")
        check.pending = [(read_810(an_810("INV-A", self.po_number, "60", price="99.00")), False)]
        check.shipped = {self.po_number: {"00010": Decimal("100")}}
        [blocked] = check.run()
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(check.already_billed(self.po_number, "00010"), Decimal(0))

    def test_a_restarted_check_has_forgotten_and_one_that_asks_sap_has_not(self):
        """The limit of memory, stated, and the check that does not have it."""
        self.assertTrue(send_order(self.sap, EDI, self.po_number, sender="ACME")["accepted"])
        [first] = InvoiceCheck(self.sap, EDI, "ACME").run()
        self.assertEqual(first["status"], "posted")

        forgetful = InvoiceCheck(self.sap, EDI, "ACME")
        self.assertEqual(self.problems(forgetful, "INV-B", "10"), [])

        asks = DurableInvoiceCheck(self.sap, EDI, "ACME")
        self.assertEqual(asks.already_billed(self.po_number, "00010"), Decimal("100"))
        self.assertEqual(self.problems(asks, "INV-B", "10"),
                         ["item 00010 bills 10, with 100 already billed, ordered 100"])


class WhenSapCannotBeAsked(unittest.TestCase):
    """#13: an invoice taken out of the mailbox is kept until SAP has dealt with it."""

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.check = InvoiceCheck(self.sap, EDI, our_id="ACME")
        self.po = one_line_order(self.sap, "100")
        self.assertTrue(send_order(self.sap, EDI, self.po, sender="ACME")["accepted"])

    def sap_fails_once(self, method, match):
        control(SAP, "POST", "/_mock/faults", {
            "match": match, "method": method, "status": 503,
            "message": "No dialog work process available", "count": 1})

    def invoice_idocs(self):
        return control(SAP, "GET", "/_mock/idocs?mestyp=INVOIC")["results"]

    def posted_ones(self):
        return [i for i in self.invoice_idocs() if i["status"] == "53"]

    def no_answer_once(self, method, match, deliver=False):
        """SAP's answer to one request is lost. With `deliver`, after SAP acted."""
        real, state = self.sap.request, {"done": False}

        def request(verb, path, body=None, content_type="application/json"):
            if verb == method and match in path and not state["done"]:
                state["done"] = True
                if deliver:
                    real(verb, path, body, content_type)
                raise urllib.error.URLError("timed out")
            return real(verb, path, body, content_type)
        return unittest.mock.patch.object(self.sap, "request", request)

    def test_an_order_sap_cannot_show_is_asked_about_again(self):
        self.sap_fails_once("GET", "A_PurchaseOrder")
        [waiting] = self.check.run()
        self.assertEqual(waiting["status"], "waiting")
        self.assertIn("answered 503 to reading purchase order %s" % self.po,
                      waiting["problems"][0])
        self.assertEqual(len(self.check.pending), 1)
        self.assertEqual(self.invoice_idocs(), [])

        [posted] = self.check.run()                 # SAP is back; the mailbox is empty
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(self.check.pending, [])
        self.assertEqual(len(self.posted_ones()), 1)

    def test_an_idoc_sap_would_not_take_is_sent_again(self):
        self.sap_fails_once("POST", "/sap/bc/idoc")
        [waiting] = self.check.run()
        self.assertEqual(waiting["status"], "waiting")
        self.assertIn("answered 503 to the invoice IDoc", waiting["problems"][0])
        [posted] = self.check.run()
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(len(self.posted_ones()), 1)
        self.assertEqual(self.check.run(), [], "and it is not kept after that")

    def test_no_answer_about_the_order_is_asked_again(self):
        with self.no_answer_once("GET", "A_PurchaseOrder"):
            [waiting] = self.check.run()
            self.assertEqual(waiting["status"], "waiting")
            self.assertIn("did not answer about purchase order", waiting["problems"][0])
            [posted] = self.check.run()
        self.assertEqual(posted["status"], "posted")

    def test_no_answer_to_the_idoc_is_not_sent_again_by_a_check_that_cannot_ask(self):
        """It may have arrived, and SAP takes a second copy without complaint."""
        with self.no_answer_once("POST", "/sap/bc/idoc", deliver=True):
            [waiting] = self.check.run()
            self.assertEqual(waiting["status"], "waiting")
            self.assertIn("did not answer the invoice IDoc", waiting["problems"][0])
            [blocked] = self.check.run()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("may hold it already; it was not sent again", blocked["problems"][0])
        self.assertEqual(self.check.pending, [], "said once, and left to a person")
        self.assertEqual(len(self.posted_ones()), 1, "SAP had taken it: one, not two")

    def test_a_check_that_asks_sap_finds_it_there_and_does_not_send_it_again(self):
        self.check = DurableInvoiceCheck(self.sap, EDI, our_id="ACME")
        with self.no_answer_once("POST", "/sap/bc/idoc", deliver=True):
            self.assertEqual(self.check.run()[0]["status"], "waiting")
            [blocked] = self.check.run()
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("is already in SAP", blocked["problems"][0])
        self.assertEqual(len(self.posted_ones()), 1)

    def test_a_check_that_asks_sap_sends_it_again_when_it_never_arrived(self):
        self.check = DurableInvoiceCheck(self.sap, EDI, our_id="ACME")
        with self.no_answer_once("POST", "/sap/bc/idoc", deliver=False):
            self.assertEqual(self.check.run()[0]["status"], "waiting")
            [posted] = self.check.run()
        self.assertEqual(posted["status"], "posted")
        self.assertEqual(len(self.posted_ones()), 1)

    def test_an_order_sap_does_not_have_is_blocked_and_the_rest_carry_on(self):
        """One invoice SAP cannot place must not take the others down with it."""
        self.check.pending = [(read_810(an_810("INV-X", "4599999999", "1")), False)]
        results = {r["invoice"]: r for r in self.check.run()}
        self.assertEqual(results["INV-X"]["status"], "blocked")
        self.assertIn("answered 404 to reading purchase order 4599999999",
                      results["INV-X"]["problems"][0])
        [other] = [r for number, r in results.items() if number != "INV-X"]
        self.assertEqual(other["status"], "posted")
        self.assertEqual(self.check.pending, [])

    def test_the_ship_notice_is_still_there_for_the_second_try(self):
        self.sap_fails_once("GET", "A_PurchaseOrder")
        self.check.run()
        self.assertEqual(self.check.shipped[self.po], {"00010": Decimal("100")})


if __name__ == "__main__":
    unittest.main()
