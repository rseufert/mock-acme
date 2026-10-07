"""Integration tests for order_to_cash, against mock-sap, mock-bank and two of
mock-einvoice.

    python3 -m unittest -v tests.test_order_to_cash

Two of mock-einvoice because an invoice crosses a network here: one is ACME's
own access point, which sends, and the other is the customer, which takes the
invoice in and answers. Each is started knowing where the other is.
"""
import datetime
import json
import os
import re
import unittest
import unittest.mock
import urllib.error
import urllib.request
from decimal import Decimal

from mockacme.invoice_check import Sap
from mockacme.order_to_cash import OrderToCash, invoice_xml, price_of

from . import another, free_port

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
BANK = os.environ.get("BANK_URL", "http://127.0.0.1:8090")

_ours, _theirs = free_port(), free_port()
ACCESS_POINT = another("mockeinvoice", [
    "--buyer-url", "http://127.0.0.1:%d/invoices" % _theirs], port=_ours)
CUSTOMER = another("mockeinvoice", [
    "--seller-url", "http://127.0.0.1:%d/responses" % _ours, "--answers", "never",
    "--clock", "2026-10-05T12:00"], port=_theirs)

# Who is selling. mock-sap has no company code master, so none of this is
# SAP's: it is what the middleware is configured with. The rate is the one
# mock-sap bills at, which is a constant of the mock's and no country's.
ACME = {"name": "ACME Corporation", "iban": "NL41MOCK0000000001", "bic": "MOCKNL2A",
        "vat": "NL123456789B01", "endpoint": ("0106", "12345678"),
        "legal_id": ("0106", "12345678"), "street": "Keizersgracht 1",
        "city": "Amsterdam", "postal_code": "1015 CJ", "country": "NL",
        "tax_rate": "19"}
# mock-sap's seeded sales order 4712 is this customer's.
ORDER, BUYER = "0000004712", "1000006"
CUSTOMERS = {BUYER: {"endpoint": ("0088", "4098765000003")}}
PAYER = {"name": "A Customer Ltd", "iban": "DE89370400440532013000", "bic": "COBADEFFXXX"}


def control(base, method, path, body=None):
    request = urllib.request.Request(
        base + path, method=method, data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read() or "null")


def closed_port():
    return "http://127.0.0.1:%d" % free_port()


class Case(unittest.TestCase):
    """Everything reset, and sales order 4712 billed in SAP."""

    def setUp(self):
        for base in (SAP, BANK, ACCESS_POINT, CUSTOMER):
            control(base, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.o2c = self.middleware()
        self.number = self.bill()

    def middleware(self, company=ACME, customers=CUSTOMERS, access_point=ACCESS_POINT):
        return OrderToCash(SAP, access_point, BANK, company, customers)

    def bill(self, order=ORDER):
        """What somebody in SAP does: bill a sales order. The INVOIC it also
        writes is not used; the billing document and its receivable are."""
        return self.sap.request("POST", "/sap/bc/idoc/generate",
                                {"mestyp": "INVOIC", "SalesOrder": order})["billing_document"]

    def billing(self, number=None):
        return self.sap.request("GET", "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV/"
                                "A_BillingDocument('%s')?$format=json"
                                % (number or self.number))["d"]

    def gross(self):
        return Decimal(self.billing()["TotalGrossAmount"]).quantize(Decimal("0.01"))

    def open_receivable(self, number=None):
        rows = self.o2c.receivable(self.billing(number)["AccountingDocument"])
        return [row for row in rows if not row["ClearingAccountingDocument"]]

    def held(self):
        """What the customer's side holds."""
        return control(CUSTOMER, "GET", "/_mock/invoices")

    def customer_says(self, code, **more):
        [invoice] = [i for i in self.held() if i["number"] == self.number]
        return control(CUSTOMER, "POST", "/_mock/invoices/%s/responses" % invoice["id"],
                       dict(more, code=code))

    def customer_pays(self, amount, **quoting):
        """Money arrives at ACME's bank, described the way this payer did, and
        the bank's days pass until it is on a statement. Returns `bank`'s."""
        control(BANK, "POST", "/_mock/credits", dict(
            quoting, account="ACME", amount=int(amount * 100), debtor=PAYER))
        today = datetime.date.fromisoformat(
            control(BANK, "GET", "/_mock/state")["clock"]["date"])
        later = today + datetime.timedelta(days=5)
        control(BANK, "POST", "/_mock/advance?to=%s" % later.isoformat())
        return self.o2c.bank(later)

    def followed(self):
        [result] = [r for r in self.o2c.follow() if r["invoice"] == self.number]
        return result


class FromBillingDocumentToPaid(Case):

    def test_billed_sent_accepted_and_paid(self):
        [sent] = self.o2c.send([self.number])
        self.assertEqual(sent, {"invoice": self.number, "customer": BUYER,
                                "status": "sent", "problems": []})
        [invoice] = self.held()
        self.assertEqual((invoice["number"], invoice["verdict"], invoice["specification"]),
                         (self.number, "valid", "peppol"))
        self.assertEqual(self.followed(), {
            "invoice": self.number, "customer_says": "", "receivable": "open",
            "status": "sent", "problems": []})

        self.customer_says("AB")
        self.assertEqual(self.followed()["status"], "received")
        self.customer_says("AP")
        self.assertEqual((self.followed()["status"], self.followed()["receivable"]),
                         ("accepted", "open"))

        banked = self.customer_pays(self.gross(), reference=self.number)
        self.assertEqual(banked["problems"], [])
        [cleared] = banked["cleared"]
        self.assertEqual(cleared["ACCOUNTINGDOCUMENT"], self.billing()["AccountingDocument"])
        self.assertEqual(self.open_receivable(), [])
        self.assertEqual(self.followed(), {
            "invoice": self.number, "customer_says": "AP", "receivable": "cleared",
            "status": "paid", "problems": []})

    def test_the_invoice_says_what_sap_holds(self):
        self.o2c.send([self.number])
        [invoice] = self.held()
        xml = urllib.request.urlopen(
            CUSTOMER + "/_mock/invoices/%s/document" % invoice["id"]).read().decode()
        billing = self.sap.request(
            "GET", "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV/A_BillingDocument('%s')"
            "?$expand=to_Item&$format=json" % self.number)["d"]
        [item] = billing["to_Item"]["results"]
        [owed] = self.o2c.receivable(billing["AccountingDocument"])

        def said(pattern):
            return re.search(pattern, xml).group(1)

        self.assertEqual(said(r"<cbc:ID>([^<]+)</cbc:ID><cbc:IssueDate>"), self.number)
        # The customer's own order number, from the sales order.
        self.assertEqual(said(r"<cac:OrderReference><cbc:ID>([^<]+)<"), "PO-50306")
        self.assertEqual(said(r"<cbc:SalesOrderID>([^<]+)<"), ORDER)
        # Due when SAP's receivable is due.
        self.assertEqual(said(r"<cbc:DueDate>([^<]+)<"),
                         "%s" % (datetime.datetime(1970, 1, 1) + datetime.timedelta(
                             milliseconds=int(re.search(r"\d+", owed["NetDueDate"]).group()))
                         ).date())
        self.assertEqual(said(r"<cbc:PaymentID>([^<]+)<"), self.number)
        self.assertEqual(said(r"<cbc:PayableAmount currencyID=\"EUR\">([^<]+)<"),
                         "%s" % self.gross())
        self.assertEqual(Decimal(said(r"<cbc:TaxExclusiveAmount[^>]*>([^<]+)<")),
                         Decimal(billing["TotalNetAmount"]))
        self.assertEqual(said(r"<cbc:InvoicedQuantity unitCode=\"H87\">([^<]+)<"), "21")
        self.assertEqual(said(r"<cac:SellersItemIdentification><cbc:ID>([^<]+)<"),
                         item["Material"])
        self.assertEqual(said(r"<cac:Item><cbc:Name>([^<]+)<"),
                         item["BillingDocumentItemText"])
        # The customer's name and address are SAP's business partner's.
        self.assertEqual(invoice["buyer"], "Meridian Trading")
        self.assertEqual(invoice["seller"], ACME["name"])

    def test_sent_once_however_often_it_is_asked(self):
        self.assertEqual([r["status"] for r in self.o2c.send([self.number])], ["sent"])
        self.assertEqual(self.o2c.send([self.number]), [])
        # Started again: nothing is kept here, and the access point is asked.
        self.assertEqual(self.middleware().send([self.number]), [])
        self.assertEqual(len(self.held()), 1)

    def test_every_invoice_sap_holds_is_looked_at_when_none_is_named(self):
        """SAP is seeded with billing documents of its own. The one customer
        that takes e-invoices gets its two; the rest are left, and said so."""
        results = {r["invoice"]: r for r in self.o2c.send()}
        mine = sorted(n for n, r in results.items() if r["customer"] == BUYER)
        self.assertGreater(len(mine), 1)        # the one billed here, and the seed's
        self.assertIn(self.number, mine)
        self.assertEqual(len(results), len(self.o2c.rows(
            "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV/A_BillingDocument")))
        self.assertEqual({results[n]["status"] for n in mine}, {"sent"})
        others = [r for r in results.values() if r["customer"] != BUYER]
        self.assertTrue(others)
        for other in others:
            self.assertEqual((other["status"], other["problems"]), ("left", [
                "customer %s is not one that takes e-invoices" % other["customer"]]))
        self.assertEqual(sorted(i["number"] for i in self.held()), mine)
        # And the second time there is only what was left to say again.
        self.assertEqual({r["status"] for r in self.o2c.send()}, {"left"})


class WhatTheCustomerSaysAndWhatTheBankSays(Case):
    """Two accounts of one invoice, reported apart."""

    def setUp(self):
        super().setUp()
        self.o2c.send([self.number])

    def test_a_customer_who_says_paid_has_not_paid(self):
        self.customer_says("AP")
        self.customer_says("PD")
        self.assertEqual(self.followed(), {
            "invoice": self.number, "customer_says": "PD", "receivable": "open",
            "status": "said paid", "problems": [
                "the customer says invoice %s is paid, and SAP holds the receivable "
                "open: no statement posted has cleared it" % self.number]})
        # And when the money does come, it is paid and there is nothing to say.
        self.customer_pays(self.gross(), reference=self.number)
        self.assertEqual((self.followed()["status"], self.followed()["problems"]),
                         ("paid", []))

    def test_a_rejection_is_passed_on(self):
        self.customer_says("RE", reasons=[{"code": "PRI", "text": "Not the agreed price."}],
                           actions=["NIN"])
        self.assertEqual(self.followed(), {
            "invoice": self.number, "customer_says": "RE", "receivable": "open",
            "status": "rejected", "problems": []})

    def test_what_the_customer_says_after_rejecting_is_not_heeded(self):
        """Peppol's guide lets a seller ignore what follows a rejection, and
        our access point does. So does this."""
        self.customer_says("RE", reasons=[{"code": "PRI", "text": "Not the agreed price."}],
                           actions=["NIN"])
        self.customer_says("AP", force=True)
        self.assertEqual((self.followed()["status"], self.followed()["customer_says"]),
                         ("rejected", "RE"))

    def test_a_receivable_in_two_items_is_paid_when_both_are_cleared(self):
        one = {"ClearingAccountingDocument": "0100000099"}
        with unittest.mock.patch.object(self.o2c, "receivable",
                                        return_value=[one, {"ClearingAccountingDocument": ""}]):
            self.assertEqual(self.followed()["receivable"], "open")
        with unittest.mock.patch.object(self.o2c, "receivable", return_value=[one, one]):
            self.assertEqual(self.followed()["status"], "paid")
        with unittest.mock.patch.object(self.o2c, "receivable", return_value=[]):
            self.assertEqual((self.followed()["receivable"], self.followed()["problems"]),
                             ("none", ["SAP holds no receivable for invoice %s"
                                       % self.number]))

    def test_a_customer_who_rejects_and_pays_all_the_same_is_said(self):
        self.customer_says("RE", reasons=[{"code": "PRI", "text": "Not the agreed price."}],
                           actions=["NIN"])
        self.customer_pays(self.gross(), reference=self.number)
        self.assertEqual(self.followed(), {
            "invoice": self.number, "customer_says": "RE", "receivable": "cleared",
            "status": "paid", "problems": [
                "the customer rejected invoice %s, and SAP holds it paid" % self.number]})

    def test_a_query_is_passed_on(self):
        self.customer_says("UQ", reasons=[{"code": "REF", "text": "Which order?"}],
                           actions=["PIN"])
        self.assertEqual(self.followed()["status"], "queried")

    def test_money_that_is_short_is_applied_to_nothing_and_said(self):
        short = self.gross() - Decimal("70.00")
        banked = self.customer_pays(short, reference=self.number)
        [problem] = banked["problems"]
        self.assertTrue(problem.startswith(
            "statement ") and " %s arrived quoting %s, and SAP applied it to nothing ("
            % (short, self.number) in problem, problem)
        self.assertEqual(banked["cleared"], [])
        self.assertEqual((self.followed()["status"], self.followed()["receivable"]),
                         ("sent", "open"))

    def test_money_quoting_nothing_we_know_is_said(self):
        banked = self.customer_pays(self.gross(), note="thanks for the widgets")
        [problem] = banked["problems"]
        self.assertIn("arrived quoting thanks for the widgets, and SAP applied it to "
                      "nothing (", problem)
        self.assertEqual(self.followed()["receivable"], "open")


class WhatTheBankStepPassesOn(unittest.TestCase):
    """`bank`, with the statements handed to it: no mock is asked."""

    def banked(self, lines, unprocessed, findings=()):
        o2c = OrderToCash(SAP, ACCESS_POINT, BANK, ACME, CUSTOMERS)

        def reconcile(run):
            run.problems.append("from the paying side")
            run.statements.append({"number": "7", "date": "2026-10-05", "lines": lines,
                                   "unprocessed": unprocessed, "cleared": [],
                                   "findings": list(findings)})
        with unittest.mock.patch.object(o2c.payments, "reconcile", reconcile):
            return o2c.bank(datetime.date(2026, 10, 5))["problems"]

    def test_only_money_arriving_is_added_and_the_paying_sides_are_kept(self):
        def line(side, returned=False):
            return {"side": side, "returned": returned, "amount": "10.00",
                    "end_to_end_id": "E2E", "reference": "REF", "note": ""}
        rows = [{"LINE": "00000%d" % n, "REASON": "why %d" % n} for n in (1, 2, 3)]
        problems = self.banked(
            [line("DBIT"), line("CRDT", returned=True), line("CRDT")],
            rows + [{"LINE": "x"}, {"LINE": "000009"}, {}], findings=["a finding"])
        self.assertEqual(problems, [
            "from the paying side",
            "statement 7 for 2026-10-05: 10.00 arrived quoting REF, and SAP applied it "
            "to nothing (why 3)",
            "statement 7 for 2026-10-05: a finding"])

    def test_money_that_quoted_nothing_at_all_says_so(self):
        [_, problem] = self.banked(
            [{"side": "CRDT", "returned": False, "amount": "10.00",
              "end_to_end_id": "NOTPROVIDED", "reference": "", "note": ""}],
            [{"LINE": "000001"}])
        self.assertEqual(problem, "statement 7 for 2026-10-05: 10.00 arrived quoting "
                                  "nothing, and SAP applied it to nothing (no reason given)")


class WhereThePayerQuotesTheInvoice(Case):
    """The payer describes the payment, so the number is where the payer put it."""

    def setUp(self):
        super().setUp()
        self.o2c.send([self.number])

    def paid_quoting(self, **quoting):
        banked = self.customer_pays(self.gross(), **quoting)
        self.assertEqual(banked["problems"], [])
        self.assertEqual(self.followed()["status"], "paid")
        [carrying] = [s for s in banked["statements"] if s["lines"]]
        return carrying["finsta"]

    def test_in_a_structured_reference_beside_a_number_of_the_payers_own(self):
        finsta = self.paid_quoting(reference=self.number, end_to_end_id="CUST-PAY-0001")
        self.assertIn("<BELNR>%s</BELNR>" % self.number, finsta)
        self.assertNotIn("CUST-PAY-0001", finsta)

    def test_in_the_note_to_payee(self):
        finsta = self.paid_quoting(note="Invoice %s, with thanks" % self.number,
                                   end_to_end_id="CUST-PAY-0002")
        self.assertIn("<BELNR></BELNR>", finsta)
        self.assertIn("<TXT01>Invoice %s, with thanks</TXT01>" % self.number, finsta)

    def test_in_the_end_to_end_id_where_nothing_else_was_written(self):
        finsta = self.paid_quoting(end_to_end_id=self.number)
        self.assertIn("<BELNR>%s</BELNR>" % self.number, finsta)

    def test_a_note_that_does_not_quote_it_is_not_helped_by_the_end_to_end_id(self):
        """Known, and in the README: remittance information is believed over
        the `EndToEndId`, so a payer who writes the number only there and
        something else in the note is not applied."""
        banked = self.customer_pays(self.gross(), note="thanks", end_to_end_id=self.number)
        self.assertEqual(len(banked["problems"]), 1)
        self.assertEqual(self.followed()["receivable"], "open")


class WhatIsNotSent(Case):

    def test_a_rate_of_tax_that_is_not_what_sap_billed_writes_nothing(self):
        o2c = self.middleware(company=dict(ACME, tax_rate="21"))
        [result] = o2c.send([self.number])
        billing = self.billing()
        self.assertEqual((result["status"], result["problems"]), ("not sent", [
            "billing document %s has tax of %s on %s, which is not the 21%% this was "
            "told to write" % (self.number,
                               Decimal(billing["TotalTaxAmount"]).quantize(Decimal("0.01")),
                               Decimal(billing["TotalNetAmount"]).quantize(Decimal("0.01")))]))
        self.assertEqual(self.held(), [])
        self.assertEqual(o2c.follow(), [])

    def test_what_our_own_access_point_will_not_send_is_refused_with_its_rules(self):
        """A seller with no VAT identifier: this writes what it was given, and
        the access point holds it to rules this did not write."""
        o2c = self.middleware(company=dict(ACME, vat=""))
        [result] = o2c.send([self.number])
        self.assertEqual(result["status"], "refused")
        [finding] = result["problems"]
        self.assertTrue(finding.startswith("BR-S-02: "), finding)
        self.assertIn("the seller has none", finding)
        self.assertEqual(self.held(), [])

    def test_only_what_stopped_it_is_passed_on_from_a_refusal(self):
        import io
        body = json.dumps({"findings": [
            {"level": "warning", "code": "W-1", "text": "a warning"},
            {"level": "fatal", "code": "BR-X", "text": "what stopped it"}]}).encode()

        def refuse(method, path, body_=None):
            if method == "POST":
                raise urllib.error.HTTPError(path, 422, "Unprocessable", {}, io.BytesIO(body))
            return []
        with unittest.mock.patch.object(self.o2c, "point", refuse):
            [result] = self.o2c.send([self.number])
        self.assertEqual((result["status"], result["problems"]),
                         ("refused", ["BR-X: what stopped it"]))

    def test_a_refusal_that_is_not_findings_is_passed_on_as_it_came(self):
        import io

        def refuse(method, path, body_=None):
            if method == "POST":
                raise urllib.error.HTTPError(path, 500, "Server Error", {},
                                             io.BytesIO(b"it fell over"))
            return []
        with unittest.mock.patch.object(self.o2c, "point", refuse):
            [result] = self.o2c.send([self.number])
        self.assertEqual((result["status"], result["problems"]), (
            "refused", ["our access point answered 500: it fell over"]))

    def test_a_billing_document_with_no_receivable_has_no_due_date_to_write(self):
        with unittest.mock.patch.object(self.o2c, "receivable", return_value=[]):
            [result] = self.o2c.send([self.number])
        self.assertEqual((result["status"], result["problems"]), ("not sent", [
            "SAP holds no receivable for billing document %s, so there is no day it "
            "falls due" % self.number]))

    def test_a_customers_side_that_does_not_answer_is_not_delivered_and_sent_again(self):
        nowhere = another("mockeinvoice", ["--buyer-url", closed_port() + "/invoices"])
        [result] = self.middleware(access_point=nowhere).send([self.number])
        self.assertEqual(result["status"], "not delivered")
        self.assertIn("to invoice %s" % self.number, result["problems"][0])
        # Not delivered is not sent: it is not followed, and it goes again.
        self.assertEqual(self.middleware(access_point=nowhere).follow(), [])
        [again] = self.middleware(access_point=nowhere).send([self.number])
        self.assertEqual(again["status"], "not delivered")

    def test_a_customer_that_takes_no_e_invoices_is_left(self):
        [result] = self.middleware(customers={}).send([self.number])
        self.assertEqual((result["status"], result["problems"]), ("left", [
            "customer %s is not one that takes e-invoices" % BUYER]))

    def test_a_billing_document_that_is_not_an_invoice_is_left(self):
        row = dict(self.billing(), BillingDocumentType="G2")
        with unittest.mock.patch.object(self.o2c, "one", return_value=row):
            [result] = self.o2c.send([self.number])
        self.assertEqual((result["status"], result["problems"]), ("left", [
            "billing document %s is of type G2, and only an invoice (F2) is written"
            % self.number]))

    def test_a_billing_document_over_two_sales_orders_is_not_written(self):
        billing = self.sap.request(
            "GET", "/sap/opu/odata/sap/API_BILLING_DOCUMENT_SRV/A_BillingDocument('%s')"
            "?$expand=to_Item&$format=json" % self.number)["d"]
        [item] = billing["to_Item"]["results"]
        billing["to_Item"]["results"] = [item, dict(item, SalesDocument="0000004713")]
        with unittest.mock.patch.object(self.o2c, "one", return_value=billing):
            xml, problems, _ = self.o2c.write(self.number)
        self.assertEqual((xml, problems), ("", [
            "billing document %s bills sales orders 0000004712, 0000004713, and this "
            "writes an invoice for one sales order" % self.number]))


class WritingTheInvoice(unittest.TestCase):
    """`invoice_xml` and what it refuses, from rows handed to it."""

    BILLING = {
        "BillingDocument": "0090000042", "TransactionCurrency": "EUR",
        "SoldToParty": BUYER, "BillingDocumentDate": "/Date(1759708800000)/",
        "TotalNetAmount": "100.000", "TotalTaxAmount": "19.000",
        "TotalGrossAmount": "119.000", "to_Item": {"results": [{
            "BillingDocumentItem": "000010", "Material": "TG31",
            "BillingDocumentItemText": "Player & <case>", "BillingQuantity": "3.000",
            "BillingQuantityUnit": "PC", "NetAmount": "100.000"}]}}
    ORDER = {"SalesOrder": ORDER, "PurchaseOrderByCustomer": "PO-1"}
    PARTNER = {"BusinessPartnerFullName": "Meridian Trading", "BusinessPartnerName": ""}
    ADDRESS = {"StreetName": "Main St", "HouseNumber": "5", "CityName": "Berlin",
               "PostalCode": "10115", "Country": "DE"}
    DUE = datetime.date(2026, 11, 5)

    def write(self, billing=None, order=None, address=None, due=DUE, company=ACME,
              customer=None):
        return invoice_xml(billing or self.BILLING, order or self.ORDER, self.PARTNER,
                           self.ADDRESS if address is None else address, due, company,
                           customer or CUSTOMERS[BUYER])

    def changed(self, **item):
        billing = json.loads(json.dumps(self.BILLING))
        header = {k: item.pop(k) for k in list(item) if k.startswith("Total")}
        billing.update(header)
        billing["to_Item"]["results"][0].update(item)
        return billing

    def test_what_it_writes_is_escaped_and_dated(self):
        xml, problems = self.write()
        self.assertEqual(problems, [])
        self.assertIn("<cbc:Name>Player &amp; &lt;case&gt;</cbc:Name>", xml)
        self.assertIn("<cbc:IssueDate>2025-10-06</cbc:IssueDate>"
                      "<cbc:DueDate>2026-11-05</cbc:DueDate>", xml)
        self.assertIn("<cbc:StreetName>Main St 5</cbc:StreetName>", xml)

    def test_a_price_that_does_not_end_is_the_lines_net_for_its_whole_quantity(self):
        xml, _ = self.write()
        self.assertIn("<cbc:PriceAmount currencyID=\"EUR\">100.00</cbc:PriceAmount>"
                      "<cbc:BaseQuantity unitCode=\"H87\">3</cbc:BaseQuantity>", xml)
        self.assertEqual(price_of(Decimal("100.00"), Decimal("3")), ("100.00", "3"))
        self.assertEqual(price_of(Decimal("24786.72"), Decimal("21")), ("1180.32", ""))
        self.assertEqual(price_of(Decimal("1.00"), Decimal("8")), ("0.125", ""))
        self.assertEqual(price_of(Decimal("10.00"), Decimal("2.5")), ("4.00", ""))

    def test_a_euro_invoice_asks_for_a_sepa_transfer_and_any_other_for_a_transfer(self):
        self.assertIn("<cbc:PaymentMeansCode>58</cbc:PaymentMeansCode>", self.write()[0])
        dollars = dict(self.BILLING, TransactionCurrency="USD")
        self.assertIn("<cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>",
                      self.write(billing=dollars)[0])

    def test_a_customers_vat_identifier_is_written_where_one_is_given(self):
        self.assertEqual(self.write()[0].count("<cac:PartyTaxScheme>"), 1)
        both = self.write(customer=dict(CUSTOMERS[BUYER], vat="DE987654321"))[0]
        self.assertEqual(both.count("<cac:PartyTaxScheme>"), 2)
        self.assertIn("<cbc:CompanyID>DE987654321</cbc:CompanyID>", both)

    def test_every_reason_it_cannot_be_written_is_given_at_once(self):
        xml, problems = self.write(
            billing=self.changed(BillingQuantityUnit="CRT", TotalTaxAmount="18.000"),
            order={"SalesOrder": ORDER, "PurchaseOrderByCustomer": " "}, address={},
            due=None)
        self.assertEqual(xml, "")
        self.assertEqual(problems, [
            "billing document 0090000042 has tax of 18.00 on 100.00, which is not the "
            "19% this was told to write",
            "billing document 0090000042: net 100.00 and tax 18.00 do not come to its "
            "gross 119.00",
            "sales order 0000004712 names no purchase order of the customer's, and an "
            "invoice has to quote one",
            "business partner 1000006 has no address in SAP",
            "SAP holds no receivable for billing document 0090000042, so there is no "
            "day it falls due",
            "item 000010 is billed in CRT, and this does not know its UN/ECE unit code"])

    def test_tax_of_more_than_the_rate_is_no_better_than_less(self):
        _, problems = self.write(billing=self.changed(
            TotalTaxAmount="20.000", TotalGrossAmount="120.000"))
        self.assertEqual(problems, ["billing document 0090000042 has tax of 20.00 on "
                                    "100.00, which is not the 19% this was told to write"])

    def test_no_tax_is_not_written_as_an_exemption_nobody_named(self):
        _, problems = self.write(billing=self.changed(
            TotalTaxAmount="0.000", TotalGrossAmount="100.000"))
        self.assertEqual(problems, [
            "billing document 0090000042 carries no tax, and which exemption that is, "
            "SAP's amounts do not say"])

    def test_items_that_do_not_come_to_the_net_are_not_written(self):
        _, problems = self.write(billing=self.changed(NetAmount="99.000"))
        self.assertEqual(problems, ["billing document 0090000042: its items come to "
                                    "99.00 and its net is 100.00"])

    def test_an_item_of_no_quantity_is_not_written(self):
        _, problems = self.write(billing=self.changed(BillingQuantity="0.000"))
        self.assertEqual(problems[0], "item 000010 bills a quantity of nothing")

    def test_half_a_cent_of_tax_is_rounded_up_as_sap_rounds_it(self):
        # 19% of 2.50 is 0.475, and SAP bills 0.48.
        billing = self.changed(NetAmount="2.500", TotalNetAmount="2.500",
                               TotalTaxAmount="0.480", TotalGrossAmount="2.980")
        self.assertEqual(self.write(billing=billing)[1], [])


if __name__ == "__main__":
    unittest.main()
