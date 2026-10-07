"""order_to_cash: SAP's billing documents sent as e-invoices, and followed to paid.

Everything else here has SAP as the buyer. This is the other half: SAP sells,
and the middleware takes what SAP billed to the customer and brings back what
became of it.

    SAP: billing document, receivable       (somebody bills a sales order)
    customer ◀──UBL Invoice──                written here, sent by the access point
    customer ──AB / AP / RE / UQ──▶          what the customer says of it
    bank: money arrives, statement
    SAP ◀──FINSTA──                          posted here; SAP clears the receivable
    paid

**The invoice is written from what SAP holds, and from nothing else unnamed.**
The billing document gives the number, the date, the currency, the lines and
the three totals. Its sales order gives the customer's own order number. The
business partner gives the customer's name and address. The receivable it
posted gives the day it falls due. What SAP does not hold, in mock-sap, is
given to `OrderToCash` and is the caller's to keep right:

  - **who is selling** (`company`): name, address, VAT identifier, Peppol
    address, bank account. mock-sap has no company code master to read.
  - **the rate of tax** (`company["tax_rate"]`). A billing document has a
    tax amount and no rate, and a rate worked back from two rounded amounts is
    a guess. So it is given, and checked: where the document's tax is not that
    rate of its net, the invoice is not written.
  - **which customers take e-invoices, and where** (`customers`): the Peppol
    address of each, and a VAT identifier if the invoice should carry one. A
    customer not listed is left alone; that is most of them, anywhere.

Nothing is invented to make a document pass. A billing document this cannot
turn into an invoice is `not sent`, with every reason.

**It is sent through an access point, and the access point is asked what
became of it.** Peppol's Invoice Response is pushed to the seller, who cannot
ask the buyer for one. So the invoice is handed to our own access point, which
holds it to its rules, delivers it, and takes the responses in; and this asks
it. The access point is mock-einvoice's supplier side
(https://github.com/rseufert/mock-einvoice), which refuses an invoice that
fails EN 16931 or Peppol's rules. The UBL here is written by hand, like every
format in this package, so that refusal is what holds the writer to something
it did not write.

**Nothing is kept here.** Whether an invoice has been sent is asked of the
access point; whether it is paid, of SAP. A process started again finds both
where they are.

**"Paid" is SAP's clearing and nothing else,** as it is in `e_invoice`. The
customer's money arrives at the bank, the bank's statement is posted to SAP,
and SAP clears the receivable the line quotes. A customer who *says* paid
(`PD`) has said something; the receivable is still open until the statement
says so too, and `follow` reports the two apart. The reverse as well: money
that arrives and that SAP applies to nothing is reported by `bank`, with SAP's
reason, because that is the cash nobody has matched.

**What the customer has to quote** is the billing document's number, which is
the invoice's number and its payment reference (`PaymentID`), and what
mock-sap clears a receivable by. See `payment_run.quoted_by` for where on a
statement line it is looked for.

**What it does not do.** Credit memos and cancellations are left alone: only
an invoice (`F2`) is written. One rate of tax, at the standard category; a
billing document with no tax is not written, because which exemption applies
is not something SAP's amounts say. A billing document over more than one
sales order is not written. A part payment is whatever SAP makes of it, which
in mock-sap is nothing: the money is reported as applied to nothing. Nobody is
reminded: an invoice past its due date is not chased.

The tests are in tests/test_order_to_cash.py.
"""
from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

from . import invoice_check, payment_run
from .payment_run import sap_date

ODATA = "/sap/opu/odata/sap"
BILLING = ODATA + "/API_BILLING_DOCUMENT_SRV/A_BillingDocument"
SALES_ORDERS = ODATA + "/API_SALES_ORDER_SRV/A_SalesOrder"
PARTNERS = ODATA + "/API_BUSINESS_PARTNER_SRV/A_BusinessPartner"
ITEMS = ODATA + "/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"

PEPPOL_BILLING = ("urn:cen.eu:en16931:2017#compliant#"
                  "urn:fdc:peppol.eu:2017:poacc:billing:3.0")
BILLING_PROFILE = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
# SAP's billing type for an invoice. Credit memos (G2), cancellations (S1) and
# the rest are other documents, which this does not write.
INVOICE_TYPE = "F2"
# SAP's unit of measure as the UN/ECE Recommendation 20 code an e-invoice must
# use. Only what is known is here; a unit that is not is a reason not to send.
UNITS = {"PC": "H87", "EA": "EA", "KG": "KGM", "L": "LTR", "M": "MTR", "H": "HUR"}
# UNCL 4461: a SEPA credit transfer, and a credit transfer that is not one.
SEPA_TRANSFER, CREDIT_TRANSFER = "58", "30"
CENT = Decimal("0.01")

# What the customer last said, as the word `follow` uses for it. A status
# that is not here is passed on as it came.
SAID = {"": "sent", "AB": "received", "IP": "received", "UQ": "queried",
        "CA": "accepted", "AP": "accepted", "RE": "rejected", "PD": "said paid"}


def cents(raw) -> Decimal:
    """An amount as SAP gives it, `24786.720`, to the cent an invoice is in."""
    return Decimal(str(raw)).quantize(CENT, rounding=ROUND_HALF_UP)


def plain(number: Decimal) -> str:
    """A quantity or a rate without the zeros SAP pads it with: 21, not 21.000."""
    text = format(number.normalize(), "f")
    return text


def price_of(net: Decimal, quantity: Decimal) -> Tuple[str, str]:
    """The price of one, and the quantity that price is for.

    An invoice line has to come to its net amount from its quantity and its
    price. SAP keeps the net and the quantity, and the price of one is their
    quotient, which need not end: 100.00 for 3. Where it ends within six
    places it is the price of one. Where it does not, the price given is the
    line's own net for the whole quantity (`BaseQuantity`), which is exact,
    and not a rounded price that no longer multiplies back.
    """
    one = net / quantity
    for places in range(2, 7):
        rounded = one.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        if rounded * quantity == net:
            return format(rounded, "f"), ""
    return format(net, "f"), plain(quantity)


def invoice_xml(billing: Dict, order: Dict, partner: Dict, address: Dict,
                due: Optional[datetime.date], company: Dict, customer: Dict
                ) -> Tuple[str, List[str]]:
    """A billing document as a Peppol BIS Billing 3.0 invoice, and why not.

    Returns the invoice and no reasons, or "" and every reason it was not
    written. The arguments are SAP's rows as OData gave them, the day the
    receivable falls due, and the two things SAP does not hold: who is
    selling, and where this customer takes its invoices.
    """
    number, currency = billing["BillingDocument"], billing["TransactionCurrency"]
    net, tax = cents(billing["TotalNetAmount"]), cents(billing["TotalTaxAmount"])
    gross = cents(billing["TotalGrossAmount"])
    rate = Decimal(str(company["tax_rate"]))
    problems = []

    if not tax:
        problems.append("billing document %s carries no tax, and which exemption "
                        "that is, SAP's amounts do not say" % number)
    elif (net * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP) != tax:
        problems.append("billing document %s has tax of %s on %s, which is not the %s%% "
                        "this was told to write" % (number, tax, net, plain(rate)))
    if net + tax != gross:
        problems.append("billing document %s: net %s and tax %s do not come to its "
                        "gross %s" % (number, net, tax, gross))
    reference = (order.get("PurchaseOrderByCustomer") or "").strip()
    if not reference:
        problems.append("sales order %s names no purchase order of the customer's, and "
                        "an invoice has to quote one" % order.get("SalesOrder", ""))
    if not address:
        problems.append("business partner %s has no address in SAP" % billing["SoldToParty"])
    if due is None:
        problems.append("SAP holds no receivable for billing document %s, so there is "
                        "no day it falls due" % number)

    lines, added = [], Decimal(0)
    for item in billing["to_Item"]["results"]:
        position = item["BillingDocumentItem"]
        quantity, amount = Decimal(item["BillingQuantity"]), cents(item["NetAmount"])
        unit = UNITS.get(item["BillingQuantityUnit"])
        if unit is None:
            problems.append("item %s is billed in %s, and this does not know its "
                            "UN/ECE unit code" % (position, item["BillingQuantityUnit"]))
        if not quantity:
            problems.append("item %s bills a quantity of nothing" % position)
            continue
        added += amount
        price, per = price_of(amount, quantity)
        lines.append(
            "<cac:InvoiceLine><cbc:ID>%s</cbc:ID>"
            "<cbc:InvoicedQuantity unitCode=%s>%s</cbc:InvoicedQuantity>"
            "<cbc:LineExtensionAmount currencyID=%s>%s</cbc:LineExtensionAmount>"
            "<cac:Item><cbc:Name>%s</cbc:Name>"
            "<cac:SellersItemIdentification><cbc:ID>%s</cbc:ID>"
            "</cac:SellersItemIdentification>"
            "<cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>%s</cbc:Percent>"
            "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>"
            "</cac:ClassifiedTaxCategory></cac:Item>"
            "<cac:Price><cbc:PriceAmount currencyID=%s>%s</cbc:PriceAmount>%s</cac:Price>"
            "</cac:InvoiceLine>" % (
                escape(position), quoteattr(unit or ""), plain(quantity),
                quoteattr(currency), amount,
                escape(item["BillingDocumentItemText"] or item["Material"]),
                escape(item["Material"]), plain(rate), quoteattr(currency), price,
                "<cbc:BaseQuantity unitCode=%s>%s</cbc:BaseQuantity>"
                % (quoteattr(unit or ""), per) if per else ""))
    if added != net:
        problems.append("billing document %s: its items come to %s and its net is %s"
                        % (number, added, net))
    if problems:
        return "", problems

    def endpoint(pair) -> str:
        return "<cbc:EndpointID schemeID=%s>%s</cbc:EndpointID>" % (
            quoteattr(pair[0]), escape(pair[1]))

    def postal(street: str, city: str, code: str, country: str) -> str:
        return ("<cac:PostalAddress><cbc:StreetName>%s</cbc:StreetName>"
                "<cbc:CityName>%s</cbc:CityName><cbc:PostalZone>%s</cbc:PostalZone>"
                "<cac:Country><cbc:IdentificationCode>%s</cbc:IdentificationCode>"
                "</cac:Country></cac:PostalAddress>"
                % (escape(street), escape(city), escape(code), escape(country)))

    def vat(identifier: str) -> str:
        return ("<cac:PartyTaxScheme><cbc:CompanyID>%s</cbc:CompanyID><cac:TaxScheme>"
                "<cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>"
                % escape(identifier)) if identifier else ""

    def legal(name: str, identifier=None) -> str:
        return ("<cac:PartyLegalEntity><cbc:RegistrationName>%s</cbc:RegistrationName>%s"
                "</cac:PartyLegalEntity>" % (escape(name), (
                    "<cbc:CompanyID schemeID=%s>%s</cbc:CompanyID>"
                    % (quoteattr(identifier[0]), escape(identifier[1])))
                    if identifier else ""))

    seller = "%s<cac:PartyName><cbc:Name>%s</cbc:Name></cac:PartyName>%s%s%s" % (
        endpoint(company["endpoint"]), escape(company["name"]),
        postal(company["street"], company["city"], company["postal_code"],
               company["country"]),
        vat(company["vat"]), legal(company["name"], company.get("legal_id")))
    name = partner["BusinessPartnerFullName"] or partner["BusinessPartnerName"]
    buyer = "%s<cac:PartyName><cbc:Name>%s</cbc:Name></cac:PartyName>%s%s%s" % (
        endpoint(customer["endpoint"]), escape(name),
        postal(" ".join(part for part in (address["StreetName"], address["HouseNumber"])
                        if part), address["CityName"], address["PostalCode"],
               address["Country"]),
        vat(customer.get("vat", "")), legal(name, customer.get("legal_id")))
    sepa = currency == "EUR" and bool(company.get("iban"))
    money = "<cbc:%%s currencyID=%s>%%s</cbc:%%s>" % quoteattr(currency)

    def amount(element: str, value: Decimal) -> str:
        return money % (element, value, element)

    return "".join([
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" '
        'xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:'
        'CommonAggregateComponents-2" '
        'xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:'
        'CommonBasicComponents-2">',
        "<cbc:CustomizationID>%s</cbc:CustomizationID>" % PEPPOL_BILLING,
        "<cbc:ProfileID>%s</cbc:ProfileID>" % BILLING_PROFILE,
        "<cbc:ID>%s</cbc:ID>" % escape(number),
        "<cbc:IssueDate>%s</cbc:IssueDate>" % sap_date(billing["BillingDocumentDate"]),
        "<cbc:DueDate>%s</cbc:DueDate>" % due,
        "<cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>",
        "<cbc:DocumentCurrencyCode>%s</cbc:DocumentCurrencyCode>" % escape(currency),
        "<cac:OrderReference><cbc:ID>%s</cbc:ID><cbc:SalesOrderID>%s</cbc:SalesOrderID>"
        "</cac:OrderReference>" % (escape(reference), escape(order["SalesOrder"])),
        "<cac:AccountingSupplierParty><cac:Party>%s</cac:Party>"
        "</cac:AccountingSupplierParty>" % seller,
        "<cac:AccountingCustomerParty><cac:Party>%s</cac:Party>"
        "</cac:AccountingCustomerParty>" % buyer,
        # The payment reference is the invoice's own number: what the customer
        # is asked to quote, and what SAP clears the receivable by.
        "<cac:PaymentMeans><cbc:PaymentMeansCode>%s</cbc:PaymentMeansCode>"
        "<cbc:PaymentID>%s</cbc:PaymentID><cac:PayeeFinancialAccount><cbc:ID>%s</cbc:ID>"
        "<cbc:Name>%s</cbc:Name>%s</cac:PayeeFinancialAccount></cac:PaymentMeans>" % (
            SEPA_TRANSFER if sepa else CREDIT_TRANSFER, escape(number),
            escape(company["iban"]), escape(company["name"]),
            "<cac:FinancialInstitutionBranch><cbc:ID>%s</cbc:ID>"
            "</cac:FinancialInstitutionBranch>" % escape(company["bic"])
            if company.get("bic") else ""),
        "<cac:TaxTotal>%s<cac:TaxSubtotal>%s%s<cac:TaxCategory><cbc:ID>S</cbc:ID>"
        "<cbc:Percent>%s</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>"
        "</cac:TaxCategory></cac:TaxSubtotal></cac:TaxTotal>" % (
            amount("TaxAmount", tax), amount("TaxableAmount", net),
            amount("TaxAmount", tax), plain(rate)),
        "<cac:LegalMonetaryTotal>%s%s%s%s</cac:LegalMonetaryTotal>" % (
            amount("LineExtensionAmount", net), amount("TaxExclusiveAmount", net),
            amount("TaxInclusiveAmount", gross), amount("PayableAmount", gross)),
        "".join(lines), "</Invoice>"]), []


class OrderToCash:
    """SAP's billing documents, sent and followed.

    `company` is who is selling and `customers` is who takes e-invoices, by
    SAP's customer number; the module's docstring says what each must hold.
    `access_point` is our own, which sends and is answered. `bank` is where
    the statements are collected from.
    """

    def __init__(self, sap: str, access_point: str, bank: str, company: Dict,
                 customers: Dict[str, Dict]):
        self.sap = invoice_check.Sap(sap)
        self.access_point = access_point.rstrip("/")
        self.company, self.customers = company, customers
        self.payments = payment_run.PaymentRun(sap, bank, company)

    # -- asking ---------------------------------------------------------------------

    def rows(self, path: str, **query) -> List[Dict]:
        query["$format"] = "json"
        found = self.sap.request("GET", "%s?%s" % (path, urllib.parse.urlencode(query)))
        return found["d"]["results"]

    def one(self, path: str, **query) -> Dict:
        query["$format"] = "json"
        return self.sap.request("GET", "%s?%s" % (path, urllib.parse.urlencode(query)))["d"]

    def point(self, method: str, path: str, body: Optional[bytes] = None):
        request = urllib.request.Request(self.access_point + path, data=body, method=method,
                                         headers={"Content-Type": "application/xml"})
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read())

    def delivered(self) -> Dict[str, Dict]:
        """The invoices our access point holds as delivered, by their number.
        One that was sent and did not arrive is not here, and is sent again."""
        return {sent["number"]: sent for sent in self.point("GET", "/_mock/sent")
                if sent["kind"] == "Invoice"
                and (sent.get("delivery") or {}).get("status") == 201}

    def receivable(self, accounting_document: str) -> List[Dict]:
        """The customer items the billing document posted."""
        return self.rows(ITEMS, **{"$filter": (
            "AccountingDocument eq '%s' and AccountingDocumentItemType eq 'D'"
            % accounting_document)})

    # -- sending --------------------------------------------------------------------

    def write(self, number: str) -> Tuple[str, List[str], Dict]:
        """One billing document as an invoice: the XML, why not, and SAP's row."""
        billing = self.one("%s('%s')" % (BILLING, number), **{"$expand": "to_Item"})
        orders = sorted({item["SalesDocument"] for item in billing["to_Item"]["results"]
                         if item["SalesDocument"]})
        if len(orders) != 1:
            return "", ["billing document %s bills %s, and this writes an invoice for "
                        "one sales order" % (number, "sales orders %s" % ", ".join(orders)
                                             if orders else "no sales order")], billing
        order = self.one("%s('%s')" % (SALES_ORDERS, orders[0]))
        partner = self.one("%s('%s')" % (PARTNERS, billing["SoldToParty"]),
                           **{"$expand": "to_BusinessPartnerAddress"})
        addresses = partner["to_BusinessPartnerAddress"]["results"]
        owed = self.receivable(billing["AccountingDocument"])
        due = sap_date(owed[0]["NetDueDate"]) if owed else None
        xml, problems = invoice_xml(billing, order, partner,
                                    addresses[0] if addresses else {}, due, self.company,
                                    self.customers[billing["SoldToParty"]])
        return xml, problems, billing

    def send(self, numbers: Optional[List[str]] = None) -> List[Dict]:
        """Send the billing documents that have not been delivered.

        Every invoice SAP holds, or the ones named. One result for each that
        was looked at: `sent`; `left`, where it is not this module's to send;
        `not sent`, where it could not be written; `refused`, where our own
        access point would not send what was written; `not delivered`, where
        the customer's side did not take it. A document already delivered has
        no result: there is nothing to say of it twice.
        """
        if numbers is None:
            numbers = [row["BillingDocument"] for row in self.rows(BILLING)]
        delivered = self.delivered()
        results = []
        for number in sorted(numbers):
            if number in delivered:
                continue
            row = self.one("%s('%s')" % (BILLING, number))
            result = {"invoice": number, "customer": row["SoldToParty"], "problems": []}
            results.append(result)
            if row["BillingDocumentType"] != INVOICE_TYPE:
                result.update(status="left", problems=[
                    "billing document %s is of type %s, and only an invoice (%s) is "
                    "written" % (number, row["BillingDocumentType"], INVOICE_TYPE)])
                continue
            if row["SoldToParty"] not in self.customers:
                result.update(status="left", problems=[
                    "customer %s is not one that takes e-invoices" % row["SoldToParty"]])
                continue
            xml, problems, _ = self.write(number)
            if problems:
                result.update(status="not sent", problems=problems)
                continue
            try:
                sent = self.point("POST", "/_mock/sent", xml.encode("utf-8"))
            except urllib.error.HTTPError as error:
                said = error.read().decode("utf-8", "replace")
                try:
                    findings = ["%s: %s" % (f.get("code", ""), f.get("text", ""))
                                for f in json.loads(said).get("findings", [])
                                if f.get("level") == "fatal"]
                except ValueError:
                    findings = []
                result.update(status="refused", problems=findings or [
                    "our access point answered %d: %s" % (error.code, said[:200])])
                continue
            delivery = sent.get("delivery") or {}
            if delivery.get("status") == 201:
                result.update(status="sent")
            else:
                result.update(status="not delivered", problems=[
                    "the customer's side answered %s to invoice %s"
                    % (delivery.get("status") or delivery.get("error")
                       or "nothing", number)])
        return results

    # -- following ------------------------------------------------------------------

    def follow(self) -> List[Dict]:
        """What became of each invoice delivered: what the customer last said,
        and whether SAP has been paid.

        `status` is `paid` when SAP has cleared the receivable, whatever was
        said. Otherwise it is what the customer said: `sent` (nothing yet),
        `received`, `accepted`, `queried`, `rejected`, or `said paid`. The two
        are reported apart in `customer_says` and `receivable`, and where they
        disagree `problems` says how.
        """
        results = []
        for number, sent in sorted(self.delivered().items()):
            heard = [one for one in self.point("GET", "/_mock/sent/%s" % sent["id"])
                     ["responses"] if not one["ignored"]]
            says = heard[-1]["code"] if heard else ""
            found = self.rows(BILLING, **{"$filter": "BillingDocument eq '%s'" % number})
            owed = self.receivable(found[0]["AccountingDocument"]) if found else []
            if not owed:
                state = "none"
            elif all(row["ClearingAccountingDocument"] for row in owed):
                state = "cleared"
            else:
                state = "open"
            result = {"invoice": number, "customer_says": says, "receivable": state,
                      "status": "paid" if state == "cleared" else SAID.get(says, says),
                      "problems": []}
            if state == "none":
                result["problems"].append(
                    "SAP holds no receivable for invoice %s" % number)
            elif state == "open" and says == "PD":
                result["problems"].append(
                    "the customer says invoice %s is paid, and SAP holds the receivable "
                    "open: no statement posted has cleared it" % number)
            elif state == "cleared" and says == "RE":
                result["problems"].append(
                    "the customer rejected invoice %s, and SAP holds it paid" % number)
            results.append(result)
        return results

    # -- the money ------------------------------------------------------------------

    def bank(self, day: datetime.date) -> Dict:
        """Post the bank's statements to SAP, and say what arrived unmatched.

        The statements are the whole account's, so this is the same posting a
        payment run does, and what it finds on the paying side is passed on
        with the rest. Added here: money that arrived and that SAP applied to
        nothing, with what the payer quoted and SAP's reason. That is a
        customer who paid short, or over, or quoted nothing we know.
        """
        run = payment_run.Run(day, "O2C", [])
        self.payments.reconcile(run)
        problems = list(run.problems)
        for record in run.statements:
            for row in record["unprocessed"]:
                try:
                    line = record["lines"][int(row.get("LINE")) - 1]
                except (TypeError, ValueError, IndexError):
                    continue
                if line["side"] != "CRDT" or line.get("returned"):
                    continue
                reference, note = payment_run.quoted_by(line)
                problems.append(
                    "statement %s for %s: %s arrived quoting %s, and SAP applied it to "
                    "nothing (%s)" % (record["number"], record["date"], line["amount"],
                                      reference or note or "nothing",
                                      row.get("REASON") or "no reason given"))
            problems.extend("statement %s for %s: %s" % (record["number"], record["date"],
                                                         finding)
                            for finding in record["findings"])
        return {"statements": run.statements, "problems": problems,
                "cleared": [row for record in run.statements for row in record["cleared"]]}
