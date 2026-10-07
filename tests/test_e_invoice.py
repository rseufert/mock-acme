"""Integration tests for e_invoice, against all four mocks.

    python3 -m unittest -v tests.test_e_invoice

`tests/__init__.py` starts the mocks, and says how to point the tests at
mocks that are already running.

The supplier here sends its order response and ship notice by EDI and its
invoice as an e-invoice: mock-edi's `no-invoice` partner, and mock-einvoice's
supplier side, which is given the invoice to send by `supplier_invoices`
below. mock-einvoice holds what it is given to the published rules before it
sends it, and holds every response it gets to Peppol's, so a test that passes
has had both documents read by something this package did not write.
"""
import datetime
import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from xml.etree import ElementTree as ET

from mockacme import e_invoice
from mockacme.e_invoice import EInvoices, read_ubl, reason_for, response_xml
from mockacme.invoice_check import InvoiceCheck, Sap

from .test_procure_to_pay import EDI, GLOBEX, SAP, PurchaseCase, control

EINVOICE = os.environ.get("EINVOICE_URL", "http://127.0.0.1:8100")
PEPPOL = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
BILLING = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
XRECHNUNG = "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
NOON = datetime.datetime(2026, 10, 7, 12, 0, 0)
VAT_RATE = Decimal("0.19")

PARTY = """<cac:%(role)s><cac:Party>
<cbc:EndpointID schemeID="0088">%(gln)s</cbc:EndpointID>
<cac:PostalAddress><cbc:StreetName>%(street)s</cbc:StreetName><cbc:CityName>%(city)s</cbc:CityName>
<cbc:PostalZone>%(zip)s</cbc:PostalZone>
<cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
<cac:PartyTaxScheme><cbc:CompanyID>%(vat)s</cbc:CompanyID>
<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
<cac:PartyLegalEntity><cbc:RegistrationName>%(name)s</cbc:RegistrationName></cac:PartyLegalEntity>
%(contact)s</cac:Party></cac:%(role)s>"""
SELLER = dict(role="AccountingSupplierParty", gln="4012345000009", street="Industriestrasse 1",
              city="Hamburg", zip="20095", vat="DE123456789", name="Globex GmbH",
              contact="<cac:Contact><cbc:Name>Accounts receivable</cbc:Name><cbc:Telephone>"
                      "+49 40 000000</cbc:Telephone><cbc:ElectronicMail>ar@globex.example"
                      "</cbc:ElectronicMail></cac:Contact>")
BUYER = dict(role="AccountingCustomerParty", gln="4098765000003", street="Hauptstrasse 5",
             city="Berlin", zip="10115", vat="DE987654321", name="ACME Corporation", contact="")
TAX_CATEGORY = ("<cbc:ID>S</cbc:ID><cbc:Percent>19</cbc:Percent>"
                "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>")


def ubl_invoice(number, po, lines, currency="EUR", issued="2026-10-02", due="2026-11-01",
                profile=BILLING, charge=None, kind="Invoice", specification=PEPPOL):
    """A Peppol invoice for an order: `lines` are (order line, quantity,
    price) and the totals are worked out from them, at 19% VAT. `charge` adds
    a freight charge, which the match does not read."""
    def amount(name, value):
        return '<cbc:%s currencyID="%s">%s</cbc:%s>' % (name, currency, value, name)

    net = sum((Decimal(quantity) * Decimal(price) for _item, quantity, price in lines),
              Decimal(0)).quantize(Decimal("0.01"))
    taxable = net + (Decimal(charge) if charge else 0)
    tax = (taxable * VAT_RATE).quantize(Decimal("0.01"))
    body = "".join(
        "<cac:%sLine><cbc:ID>%d</cbc:ID>"
        '<cbc:%s unitCode="C62">%s</cbc:%s>%s%s'
        "<cac:Item><cbc:Name>Part</cbc:Name><cac:ClassifiedTaxCategory>%s"
        "</cac:ClassifiedTaxCategory></cac:Item>"
        "<cac:Price>%s</cac:Price></cac:%sLine>"
        % (kind, index, "InvoicedQuantity" if kind == "Invoice" else "CreditedQuantity",
           quantity, "InvoicedQuantity" if kind == "Invoice" else "CreditedQuantity",
           amount("LineExtensionAmount",
                  (Decimal(quantity) * Decimal(price)).quantize(Decimal("0.01"))),
           "<cac:OrderLineReference><cbc:LineID>%s</cbc:LineID></cac:OrderLineReference>" % item
           if item else "", TAX_CATEGORY, amount("PriceAmount", price), kind)
        for index, (item, quantity, price) in enumerate(lines, 1))
    freight = ("<cac:AllowanceCharge><cbc:ChargeIndicator>true</cbc:ChargeIndicator>"
               "<cbc:AllowanceChargeReason>Freight</cbc:AllowanceChargeReason>%s"
               "<cac:TaxCategory>%s</cac:TaxCategory></cac:AllowanceCharge>"
               % (amount("Amount", charge), TAX_CATEGORY)) if charge else ""
    return ("""<?xml version="1.0" encoding="UTF-8"?>
<%(kind)s xmlns="urn:oasis:names:specification:ubl:schema:xsd:%(kind)s-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
<cbc:CustomizationID>%(peppol)s</cbc:CustomizationID><cbc:ProfileID>%(profile)s</cbc:ProfileID>
<cbc:ID>%(number)s</cbc:ID><cbc:IssueDate>%(issued)s</cbc:IssueDate>%(dates)s
<cbc:DocumentCurrencyCode>%(currency)s</cbc:DocumentCurrencyCode>
<cbc:BuyerReference>ACME-AP</cbc:BuyerReference>
%(order)s%(seller)s%(buyer)s
<cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode>
<cac:PayeeFinancialAccount><cbc:ID>DE02120300000000202051</cbc:ID></cac:PayeeFinancialAccount>
</cac:PaymentMeans>%(terms)s%(freight)s
<cac:TaxTotal>%(tax)s<cac:TaxSubtotal>%(taxable)s%(tax)s
<cac:TaxCategory>%(category)s</cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>
<cac:LegalMonetaryTotal>%(net)s%(exclusive)s%(inclusive)s%(charges)s%(payable)s
</cac:LegalMonetaryTotal>%(body)s</%(kind)s>
""" % dict(
        kind=kind, peppol=specification, profile=profile, number=number, issued=issued,
        currency=currency, body=body, freight=freight, category=TAX_CATEGORY,
        dates=("<cbc:DueDate>%s</cbc:DueDate><cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>" % due
               if kind == "Invoice" else
               "<cbc:CreditNoteTypeCode>381</cbc:CreditNoteTypeCode>"),
        terms="" if kind == "Invoice" else
              "<cac:PaymentTerms><cbc:Note>Net</cbc:Note></cac:PaymentTerms>",
        order="<cac:OrderReference><cbc:ID>%s</cbc:ID></cac:OrderReference>" % po if po else "",
        seller=PARTY % SELLER, buyer=PARTY % BUYER,
        tax=amount("TaxAmount", tax), taxable=amount("TaxableAmount", taxable),
        net=amount("LineExtensionAmount", net), exclusive=amount("TaxExclusiveAmount", taxable),
        inclusive=amount("TaxInclusiveAmount", taxable + tax),
        charges=amount("ChargeTotalAmount", charge) if charge else "",
        payable=amount("PayableAmount", taxable + tax))).encode("utf-8")


def einvoice(method, path, body=None):
    """Talk to mock-einvoice; the answer as JSON where it is JSON."""
    request = urllib.request.Request(EINVOICE + path, data=body, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            raw, kind = response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as refused:
        with refused:
            raise AssertionError("mock-einvoice answered %d to %s %s: %s" % (
                refused.code, method, path, refused.read().decode("utf-8")[:600]))
    return json.loads(raw) if kind.startswith("application/json") else raw


class EInvoiceCase(PurchaseCase):
    def setUp(self):
        super().setUp()
        control(EINVOICE, "POST", "/_mock/reset")
        # The supplier ships and sends no 810: its invoice comes another way.
        self.supplier_behaves("no-invoice")
        self.invoices = EInvoices(self.p2p.check, EINVOICE, now=lambda: NOON)

    def order_lines(self, po):
        """(item, quantity, price) of each item of an order in SAP."""
        return [(i["PurchaseOrderItem"], i["OrderQuantity"], i["NetPriceAmount"])
                for i in self.sap.purchase_order(po)["to_PurchaseOrderItem"]["results"]]

    def supplier_invoices(self, number, po, lines=None, **how):
        """The supplier sends an e-invoice for an order: for all of it, at the
        order's prices, unless told otherwise."""
        lines = self.order_lines(po) if lines is None else lines
        sent = einvoice("POST", "/_mock/sent", ubl_invoice(number, po, lines, **how))
        self.assertEqual(sent["verdict"], "valid")
        return sent["id"]

    def at_the_supplier(self, sent="1"):
        """What the supplier has heard of a document: its status, and each
        response as (code, whether it was ignored)."""
        found = einvoice("GET", "/_mock/sent/%s" % sent)
        return found["status"], [(got["code"], got["ignored"]) for got in found["responses"]]

    def response(self, number):
        """A response the supplier was given, read: its code, and its reasons
        as (code, list, text)."""
        root = ET.fromstring(einvoice("GET", "/_mock/answers/%s" % number))
        cbc, cac = e_invoice.CBC, e_invoice.CAC
        return (root.findtext(".//%sResponseCode" % cbc),
                [(status.findtext(cbc + "StatusReasonCode"),
                  status.find(cbc + "StatusReasonCode").get("listID"),
                  status.findtext(cbc + "StatusReason"))
                 for status in root.iter(cac + "Status")])


class TestOneEInvoice(EInvoiceCase):
    def test_a_matching_e_invoice_is_posted_and_the_supplier_told(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)

        [result] = self.invoices.run()

        self.assertEqual((result["invoice"], result["po"], result["status"],
                          result["problems"], result["said"]),
                         ("GLX-9001", po, "posted", [], ["AB", "AP"]))
        [item] = self.open_items()
        self.assertEqual((item["Supplier"], item["TransactionCurrency"]), (GLOBEX, "EUR"))
        self.assertEqual(abs(Decimal(item["AmountInTransactionCurrency"])),
                         Decimal("1487.50"))        # 1250.00 and 19% VAT
        self.assertEqual(self.at_the_supplier(), ("AP", [("AB", ""), ("AP", "")]))

    def test_it_is_paid_when_sap_has_cleared_it_and_not_before(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        self.invoices.run()
        self.assertEqual(self.invoices.paid(), [])

        # Sent to the bank and accepted is not paid: nothing has cleared.
        due = self.p2p.last_due_date()
        self.advance_bank_to(due)
        run = self.p2p.pay(max(due, self.bank_today()), "RUN1")
        self.assertEqual([item.status for item in run.items], ["accepted"])
        self.assertEqual(self.invoices.paid(), [])
        self.assertEqual(self.at_the_supplier()[0], "AP")

        self.advance_bank_to(self.day_after_settlement(run))
        self.p2p.reconcile(run)
        self.assertEqual(self.invoices.paid(), [
            {"invoice": "GLX-9001", "status": "paid", "said": ["AB", "AP", "PD"]}])
        self.assertEqual(self.at_the_supplier(),
                         ("PD", [("AB", ""), ("AP", ""), ("PD", "")]))
        self.assertEqual(self.response(3), ("PD", []))
        # And once: asking again says nothing more.
        self.assertEqual(self.invoices.paid(), [])
        self.assertEqual(len(self.at_the_supplier()[1]), 3)

    def test_two_orders_and_two_invoices_each_answered_for_itself(self):
        first, second = self.purchase(), self.purchase(price="13.00")
        self.supplier_invoices("GLX-9001", first)
        self.supplier_invoices("GLX-9002", second,
                               [(item, quantity, "14.00")
                                for item, quantity, _price in self.order_lines(second)])
        results = {result["invoice"]: result for result in self.invoices.run()}
        self.assertEqual((results["GLX-9001"]["said"], results["GLX-9002"]["said"]),
                         (["AB", "AP"], ["AB", "RE"]))
        self.assertEqual((self.at_the_supplier("1")[0], self.at_the_supplier("2")[0]),
                         ("AP", "RE"))

    def test_an_invoice_is_collected_once(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        self.invoices.run()
        self.assertEqual(self.invoices.run(), [])
        self.assertEqual(len(self.at_the_supplier()[1]), 2)
        self.assertEqual(len(self.open_items()), 1)

    def test_under_billing_with_response_the_answers_say_so(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po, profile=e_invoice.BILLING_WITH_RESPONSE)
        self.invoices.run()
        root = ET.fromstring(einvoice("GET", "/_mock/answers/2"))
        self.assertEqual(root.findtext(e_invoice.CBC + "ProfileID"),
                         e_invoice.BILLING_WITH_RESPONSE)


class TestWhatIsNotAccepted(EInvoiceCase):
    def refused(self, number="GLX-9001"):
        """Run, and the one result with what the supplier was told second."""
        [result] = self.invoices.run()
        self.assertEqual((result["status"], self.open_items()), ("blocked", []))
        return result, self.response(2)

    def test_a_price_that_is_not_the_orders_is_rejected(self):
        po = self.purchase()
        [(item, quantity, _price)] = self.order_lines(po)
        self.supplier_invoices("GLX-9001", po, [(item, quantity, "13.00")])
        result, told = self.refused()
        self.assertEqual(result["said"], ["AB", "RE"])
        self.assertEqual(told, ("RE", [
            ("PRI", "OPStatusReason", "item %s billed at 13.00, ordered at 12.50" % item),
            ("NIN", "OPStatusAction", None)]))
        self.assertEqual(self.at_the_supplier(), ("RE", [("AB", ""), ("RE", "")]))

    def test_more_than_was_ordered_or_shipped_is_rejected(self):
        po = self.purchase()
        [(item, _quantity, price)] = self.order_lines(po)
        self.supplier_invoices("GLX-9001", po, [(item, "101", price)])
        _result, (code, reasons) = self.refused()
        self.assertEqual((code, [reason[0] for reason in reasons]),
                         ("RE", ["QTY", "QTY", "NIN"]))
        self.assertIn("bills 101, ordered 100", reasons[0][2])
        self.assertIn("bills 101, shipped 100", reasons[1][2])

    def test_an_item_the_order_does_not_have_is_rejected(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po, self.order_lines(po) + [("00090", "1", "1.00")])
        _result, (code, reasons) = self.refused()
        self.assertEqual((code, reasons[0][:2], reasons[0][2]),
                         ("RE", ("ITM", "OPStatusReason"),
                          "item 00090 is not on purchase order %s" % po))

    def test_a_charge_is_not_read_so_the_total_does_not_add_up(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po, charge="25.00")
        _result, (code, reasons) = self.refused()
        self.assertEqual((code, reasons[0][0]), ("RE", "FIN"))
        self.assertIn("lines add up to 1250.00", reasons[0][2])

    def test_another_currency_is_rejected_as_other_with_its_text(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po, currency="USD")
        _result, (code, reasons) = self.refused()
        self.assertEqual((code, reasons[0]), ("RE", (
            "OTH", "OPStatusReason", "invoice is in USD, purchase order %s is in EUR" % po)))

    def test_an_order_sap_does_not_have_is_queried(self):
        self.supplier_invoices("GLX-9001", "4599999999", [("00010", "1", "1.00")])
        result, told = self.refused()
        self.assertEqual(result["said"], ["AB", "UQ"])
        self.assertEqual(told, ("UQ", [
            ("REF", "OPStatusReason", "SAP answered 404 to reading purchase order 4599999999"),
            ("PIN", "OPStatusAction", None)]))
        self.assertEqual(self.at_the_supplier()[0], "UQ")

    def test_no_order_named_or_no_order_line_is_queried_without_asking_sap(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", "", self.order_lines(po))
        result, (code, reasons) = self.refused()
        self.assertEqual((code, reasons[0]), ("UQ", (
            "REF", "OPStatusReason", "invoice GLX-9001 names no purchase order")))
        self.assertEqual(result["po"], "")

        [(_item, quantity, price)] = self.order_lines(po)
        self.supplier_invoices("GLX-9002", po, [("", quantity, price)])
        [result] = self.invoices.run()
        self.assertEqual((result["status"], result["problems"], result["said"]),
                         ("blocked", ["line 1 names no order line"], ["AB", "UQ"]))

    def test_two_lines_for_one_order_line_are_rejected(self):
        po = self.purchase()
        [(item, _quantity, price)] = self.order_lines(po)
        self.supplier_invoices("GLX-9001", po, [(item, "60", price), (item, "40", price)])
        _result, (code, reasons) = self.refused()
        self.assertEqual((code, reasons[0][0], reasons[0][2]),
                         ("RE", "OTH", "line 2 is the second to name order line %s" % item))

    def test_nothing_follows_a_rejection(self):
        po = self.purchase()
        [(item, quantity, _price)] = self.order_lines(po)
        self.supplier_invoices("GLX-9001", po, [(item, quantity, "13.00")])
        self.invoices.run()
        self.assertEqual((self.invoices.run(), self.invoices.paid()), ([], []))
        self.assertEqual(len(self.at_the_supplier()[1]), 2)


class TestTheSameEInvoiceTwice(EInvoiceCase):
    def test_a_copy_that_arrives_after_a_restart_is_rejected_by_asking_sap(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        self.invoices.run()

        # The middleware is started again and remembers nothing; the supplier
        # sends the invoice again.
        again = EInvoices(self.middleware().check, EINVOICE, now=lambda: NOON)
        again.taken = {"1"}
        self.supplier_invoices("GLX-9001", po)
        [result] = again.run()

        self.assertEqual((result["status"], result["problems"], result["said"]),
                         ("blocked", ["supplier invoice GLX-9001 from %s is already in SAP"
                                      % GLOBEX], ["AB", "RE"]))
        self.assertEqual(len(self.open_items()), 1)
        # The supplier's side finds an invoice by its number, and takes a
        # response to be about the later of two.
        self.assertEqual(self.at_the_supplier("1")[0], "AP")
        self.assertEqual(self.at_the_supplier("2"), ("RE", [("AB", ""), ("RE", "")]))
        self.assertEqual(self.response(4)[1][0][:2], ("REF", "OPStatusReason"))

    def test_started_again_it_answers_what_it_answered_and_the_supplier_ignores_it(self):
        """Known to be wrong, and in the README: what was collected and said
        is one process's memory."""
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        self.invoices.run()

        again = EInvoices(self.middleware().check, EINVOICE, now=lambda: NOON)
        [result] = again.run()

        self.assertEqual((result["status"], result["said"]), ("blocked", ["AB", "RE"]))
        self.assertEqual(len(self.open_items()), 1)
        # Peppol's order is what saves the supplier's record: after accepted,
        # only paid is heeded.
        self.assertEqual(self.at_the_supplier(), ("AP", [
            ("AB", ""), ("AP", ""), ("AB", "OP-BR111-R005"), ("RE", "OP-BR111-R005")]))
        # And paid is then never said: to this process the invoice is rejected.
        self.pay_what_is_due()
        self.assertEqual((again.paid(), self.at_the_supplier()[0]), ([], "AP"))

    def test_a_copy_in_the_same_run_is_left_and_said_so(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        self.supplier_invoices("GLX-9001", po)
        left, posted = self.invoices.run()
        self.assertEqual((left["status"], left["problems"], posted["status"]),
                         ("left", ["invoice GLX-9001 was collected before; this copy was "
                                   "not checked"], "posted"))
        self.assertEqual(len(self.open_items()), 1)


class TestWhatIsNotTheSuppliersToHear(EInvoiceCase):
    def test_an_invoice_sap_may_already_hold_is_blocked_and_the_supplier_not_told(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        # A check that cannot ask SAP, on a run after one whose IDoc got no answer.
        plain = InvoiceCheck(self.sap, EDI, our_id="ACME")
        invoices = EInvoices(plain, EINVOICE, now=lambda: NOON)
        invoices.collect()
        plain.pending.append((invoices.known["GLX-9001"]["invoice"], True))
        [result] = invoices.run()
        self.assertEqual((result["status"], result["said"]), ("blocked", ["AB"]))
        self.assertIn("SAP did not answer", result["problems"][0])
        self.assertIsNone(reason_for(result["problems"][0]))
        self.assertEqual(self.at_the_supplier(), ("AB", [("AB", "")]))

    def test_an_xrechnung_invoice_is_posted_and_paid_and_told_nothing(self):
        # XRechnung has no response message.
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po, specification=XRECHNUNG)
        [result] = self.invoices.run()
        self.assertEqual((result["status"], result["said"], len(self.open_items())),
                         ("posted", [], 1))
        self.pay_what_is_due()
        self.assertEqual(self.invoices.paid(),
                         [{"invoice": "GLX-9001", "status": "paid", "said": []}])
        self.assertEqual(self.at_the_supplier(), ("", []))

    def test_while_sap_does_not_answer_the_invoice_waits_and_is_then_decided(self):
        po = self.purchase()
        self.supplier_invoices("GLX-9001", po)
        check = InvoiceCheck(Sap("http://127.0.0.1:1"), EDI, our_id="ACME")
        invoices = EInvoices(check, EINVOICE, now=lambda: NOON)
        [result] = invoices.run()
        self.assertEqual(result["status"], "waiting")
        self.assertNotIn("said", result)
        self.assertEqual(self.at_the_supplier(), ("AB", [("AB", "")]))

        check.sap = self.sap
        [result] = invoices.run()
        self.assertEqual((result["status"], result["said"]), ("posted", ["AB", "AP"]))
        self.assertEqual(self.at_the_supplier()[0], "AP")

    def test_a_payable_with_no_supplier_item_is_not_cleared(self):
        self.assertFalse(self.invoices.cleared("9999999999"))

    def test_nothing_is_said_after_a_rejection_or_a_payment_whoever_asks(self):
        po = self.purchase()
        [(item, quantity, _price)] = self.order_lines(po)
        self.supplier_invoices("GLX-9001", po, [(item, quantity, "13.00")])
        self.invoices.run()
        self.invoices.say("GLX-9001", "AP")
        self.assertEqual(self.at_the_supplier(), ("RE", [("AB", ""), ("RE", "")]))
        self.assertTrue(EInvoices.final(["AB", "AP", "PD"]))
        self.assertFalse(EInvoices.final(["AB", "AP"]) or EInvoices.final([]))

    def test_a_credit_note_is_left_where_it_is(self):
        po = self.purchase()
        sent = einvoice("POST", "/_mock/sent", ubl_invoice(
            "GLX-9001-C", po, self.order_lines(po), kind="CreditNote"))
        self.assertEqual(sent["verdict"], "valid")
        [result] = self.invoices.run()
        self.assertEqual((result["status"], result["problems"]), ("left", [
            "CreditNote GLX-9001-C is not an invoice, and only invoices are checked"]))
        self.assertEqual((self.at_the_supplier(), self.open_items()), (("", []), []))


class TestReadingAndWriting(EInvoiceCase):
    INVOICE = ubl_invoice("GLX-9001", "4500000001", [("00010", "100", "12.50"),
                                                     ("00020", "2.5", "4.15")])

    def test_the_e_invoice_is_read_as_the_810_is(self):
        invoice = read_ubl(self.INVOICE)
        self.assertEqual({key: invoice[key] for key in (
            "number", "po", "date", "currency", "net_days", "lines", "tax", "total")}, {
            "number": "GLX-9001", "po": "4500000001", "date": "20261002", "currency": "EUR",
            "net_days": 30, "tax": Decimal("239.47"), "total": Decimal("1499.85"),
            "lines": {"00010": (Decimal("100"), Decimal("12.50")),
                      "00020": (Decimal("2.5"), Decimal("4.15"))}})
        self.assertEqual((invoice["type"], invoice["profile"], invoice["issued"],
                          invoice["unreadable"]), ("380", BILLING, "2026-10-02", []))
        self.assertEqual((invoice["seller"], invoice["buyer"]), (
            {"endpoint": "4012345000009", "scheme": "0088", "name": "Globex GmbH"},
            {"endpoint": "4098765000003", "scheme": "0088", "name": "ACME Corporation"}))

    def test_what_is_not_an_invoice_is_refused(self):
        for document in (b"hello", b"<a/>", ubl_invoice("C", "1", [("1", "1", "1")],
                                                         kind="CreditNote")):
            with self.assertRaises(e_invoice.NotAnInvoice):
                read_ubl(document)

    def test_what_keeps_the_match_from_being_run(self):
        text = self.INVOICE.decode("utf-8")
        for change, wanted in (
                (("<cbc:PriceAmount currencyID=\"EUR\">12.50</cbc:PriceAmount>",
                  "<cbc:PriceAmount currencyID=\"EUR\">1250.00</cbc:PriceAmount>"
                  "<cbc:BaseQuantity unitCode=\"C62\">100</cbc:BaseQuantity>"),
                 "line 1 prices 100 units at once, and the order prices one"),
                ((">2.5</cbc:InvoicedQuantity>", ">2,5</cbc:InvoicedQuantity>"),
                 "line 2's quantity is '2,5', which is not a number")):
            invoice = read_ubl(text.replace(*change).encode("utf-8"))
            self.assertEqual(invoice["unreadable"], [wanted])
            self.assertEqual(reason_for(wanted), ("OTH", "RE"))

    def test_no_due_date_is_no_terms(self):
        text = self.INVOICE.decode("utf-8").replace("<cbc:DueDate>2026-11-01</cbc:DueDate>", "")
        self.assertIsNone(read_ubl(text.encode("utf-8"))["net_days"])

    def test_every_response_this_writes_passes_peppols_rules(self):
        """Asked of mock-einvoice, which holds a response to the 82 published
        rules of the Peppol Invoice Response and wrote none of this."""
        invoice = read_ubl(self.INVOICE)
        every_reason = tuple("%s <&> \"quoted\"" % phrase for phrase, _code, _status
                             in e_invoice.REASONS)
        for code, problems in (("AB", ()), ("AP", ()), ("PD", ()), ("RE", every_reason),
                               ("UQ", every_reason[2:5])):
            found = einvoice("POST", "/_mock/validate",
                             response_xml(invoice, "R-1", code, NOON, problems))
            self.assertEqual((found["kind"], found["verdict"], found["findings"]),
                             ("ApplicationResponse", "valid", []), code)

    def test_one_with_no_reason_would_not_pass_which_is_why_each_has_one(self):
        found = einvoice("POST", "/_mock/validate",
                         response_xml(read_ubl(self.INVOICE), "R-1", "RE", NOON)
                         .replace(b'<cac:Status><cbc:StatusReasonCode listID="OPStatusAction">'
                                  b"NIN</cbc:StatusReasonCode></cac:Status>", b""))
        self.assertEqual([f["code"] for f in found["findings"]], ["PEPPOL-T111-R001"])

    def test_each_reason_has_a_code_from_peppols_list_and_a_status(self):
        codes = {"NON", "REF", "LEG", "REC", "QUA", "DEL", "PRI", "QTY", "ITM", "PAY", "UNR",
                 "FIN", "PPD", "OTH"}
        for _phrase, code, status in e_invoice.REASONS:
            self.assertIn(code, codes)
            self.assertIn(status, e_invoice.ACTIONS)
