"""e_invoice: a supplier's e-invoice matched and posted, and the supplier told.

`invoice_check` takes a supplier's invoice as an X12 810. This takes it as a
UBL e-invoice, the kind EN 16931 describes and Peppol and XRechnung carry,
and does the same with it: the three-way match, and an INVOIC IDoc into SAP
or a block with the reasons. Then it does what an 810 has no answer for. It
tells the supplier what became of the invoice, in Peppol Invoice Responses:

    supplier ──UBL Invoice──▶           collected from the supplier's side
    supplier ◀──AB──                    received
    SAP ◀──INVOIC──                     matched, posted
    supplier ◀──AP── or ◀──RE / UQ──    accepted, or not and why
    SAP clears the payable              (payment_run)
    supplier ◀──PD──                    paid

**The match is `InvoiceCheck`'s, not a copy of it.** `read_ubl` makes of the
e-invoice the dict `read_810` makes of an 810, and the check, the IDoc and
the duplicate question are whichever `InvoiceCheck` this is given.

**The ship notice still comes by EDI.** The match needs to know what was
shipped, and an e-invoice does not say. So the order goes out as an 850 and
the 856 comes back, as before, from a supplier who sends no 810: mock-edi's
`no-invoice` behaviour is that supplier. The invoice alone arrives the new way.

**"Paid" is SAP's clearing and nothing else.** `PD` is said when the payable
the invoice became is cleared in SAP, which is the fact the remittance advice
rests on too. A payment sent and not yet on a statement is not paid.

**What is said, and when.** Peppol's guide leaves the buyer to choose between
rejecting an invoice and querying it. Here an invoice only a new invoice can
cure is rejected (`RE`): a price, a quantity, an item, a total, a duplicate.
One that information could cure is queried (`UQ`): an order SAP does not
have, or none named. `REASONS` is the table. A block that is not the
supplier's doing, SAP not answering among them, is not told: the invoice
stays at received. The order of statuses is the
guide's (`OP-BR111-R012`): nothing after `RE` or `PD`, only `PD` after `AP`.

**An XRechnung invoice is matched and posted, and told nothing.** XRechnung
has no response message, so there is nothing to say it with.

**What it does not do.** Credit notes are left where they are. A part payment
is not told as one (`PD` with the reason `PPD`): a payable is cleared or it is
not. Allowances and charges are not read, so an invoice with one is rejected
because it does not add up, as an 810 with a `SAC` is blocked. What it has
collected and said is one process's memory, as `InvoiceCheck.posted` is.

The supplier is mock-einvoice (https://github.com/rseufert/mock-einvoice),
which holds every response it is given to Peppol's 82 published rules and
refuses one that fails. The UBL here is read and written by hand, like every
format in this package, so that refusal is what holds the writer to something
it did not write. The tests are in tests/test_e_invoice.py.
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from . import invoice_check

CAC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}"
CBC = "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}"
INVOICE = "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice"
RESPONSE_NS = "urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2"
# What a Peppol Invoice Response says it is, and the two processes it is part
# of: answering an invoice by choice, and answering one sent under billing's
# profile 02, where the answer is a required step.
RESPONSE = "urn:fdc:peppol.eu:poacc:trns:invoice_response:3"
ANSWERING = "urn:fdc:peppol.eu:poacc:bis:invoice_response:3"
BILLING_WITH_RESPONSE = "urn:peppol:bis:billing_with_response"
ITEMS = "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"

# Why an invoice was not accepted, read off the reason `InvoiceCheck` gave:
# (a phrase of the reason, Peppol's reason code, the status). The first that
# fits is used. Peppol's list (OPStatusReason) has no code for a duplicate or
# for a currency. A duplicate is told as a reference that is wrong, which is
# the nearest; the currency is `OTH`, which must carry its text, as every
# reason here does.
#
# A reason that fits none of these is not the supplier's to hear: SAP not
# answering, an IDoc SAP would not post. The invoice stays at received, and
# a person looks.
REASONS = (
    ("has already been posted", "REF", "RE"),
    ("is already in SAP", "REF", "RE"),
    ("names no purchase order", "REF", "UQ"),
    ("names no order line", "REF", "UQ"),
    ("to reading purchase order", "REF", "UQ"),
    ("is not on purchase order", "ITM", "RE"),
    ("billed at", "PRI", "RE"),
    ("bills", "QTY", "RE"),
    ("lines add up", "FIN", "RE"),
    ("invoice is in", "OTH", "RE"),
    ("is the second to name order line", "OTH", "RE"),
    ("units at once", "OTH", "RE"),
    ("is not a number", "OTH", "RE"),
)
# What the supplier is asked to do about it: send a new invoice, or tell us more.
ACTIONS = {"RE": "NIN", "UQ": "PIN"}


class NotAnInvoice(Exception):
    """A document that is not a UBL invoice this can read."""


def _text(element, path: str) -> str:
    found = element.find(path.replace("cac:", CAC).replace("cbc:", CBC))
    return "" if found is None or found.text is None else found.text.strip()


def _all(element, path: str):
    return element.findall(path.replace("cac:", CAC).replace("cbc:", CBC))


def _decimal(raw: str, what: str, unreadable: List[str]) -> Decimal:
    try:
        return Decimal(raw)
    except InvalidOperation:
        unreadable.append("%s is %r, which is not a number" % (what, raw))
        return Decimal(0)


def read_ubl(payload: bytes) -> dict:
    """The e-invoice as the dict `invoice_check.read_810` makes, and more.

    The match's fields: ``number`` (BT-1), ``po`` (BT-13), ``date`` (BT-2, as
    `YYYYMMDD`), ``currency`` (BT-5), ``lines`` by the order line each names
    (BT-132) as quantity (BT-129) and net price (BT-146), ``tax`` (BT-110) and
    ``total`` (BT-112, with VAT). ``net_days`` is the days from the invoice
    date to the due date (BT-9) where there is one: an e-invoice states its
    terms in words, which are not read.

    And what answering it needs: ``type`` (BT-3), ``profile`` (BT-23) and the
    two parties' electronic addresses and names.

    ``unreadable`` is what keeps the match from being run, each in words: a
    line that names no order line, or names one twice, or a price that is for
    more than one unit. Nothing is guessed around any of them.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise NotAnInvoice("not XML: %s" % error)
    if root.tag != INVOICE:
        raise NotAnInvoice("a %s, not a UBL Invoice" % root.tag.rpartition("}")[2])
    unreadable: List[str] = []
    issued, due = _text(root, "cbc:IssueDate"), _text(root, "cbc:DueDate")
    try:
        net_days = (datetime.date.fromisoformat(due)
                    - datetime.date.fromisoformat(issued)).days if due else None
    except ValueError:
        net_days = None
    lines: Dict[str, Tuple[Decimal, Decimal]] = {}
    for line in _all(root, "cac:InvoiceLine"):
        name = "line %s" % _text(line, "cbc:ID")
        item = _text(line, "cac:OrderLineReference/cbc:LineID")
        quantity = _decimal(_text(line, "cbc:InvoicedQuantity"), name + "'s quantity",
                            unreadable)
        price = _decimal(_text(line, "cac:Price/cbc:PriceAmount"), name + "'s price",
                         unreadable)
        base = _text(line, "cac:Price/cbc:BaseQuantity")
        if base and _decimal(base, name + "'s price base quantity", unreadable) != 1:
            unreadable.append("%s prices %s units at once, and the order prices one"
                              % (name, base))
        if not item:
            unreadable.append("%s names no order line" % name)
        elif item in lines:
            unreadable.append("%s is the second to name order line %s" % (name, item))
        else:
            lines[item] = (quantity, price)

    def party(path: str) -> dict:
        endpoint = root.find((path + "/cbc:EndpointID").replace("cac:", CAC)
                             .replace("cbc:", CBC))
        return {"endpoint": _text(root, path + "/cbc:EndpointID"),
                "scheme": "" if endpoint is None else endpoint.get("schemeID", ""),
                "name": _text(root, path + "/cac:PartyLegalEntity/cbc:RegistrationName")}

    return {
        "number": _text(root, "cbc:ID"), "po": _text(root, "cac:OrderReference/cbc:ID"),
        "date": issued.replace("-", ""), "issued": issued,
        "currency": _text(root, "cbc:DocumentCurrencyCode"), "net_days": net_days,
        "lines": lines,
        "tax": _decimal(_text(root, "cac:TaxTotal/cbc:TaxAmount") or "0", "the VAT",
                        unreadable),
        "total": _decimal(_text(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount") or "0",
                          "the total with VAT", unreadable),
        "type": _text(root, "cbc:InvoiceTypeCode"), "profile": _text(root, "cbc:ProfileID"),
        "seller": party("cac:AccountingSupplierParty/cac:Party"),
        "buyer": party("cac:AccountingCustomerParty/cac:Party"),
        "unreadable": unreadable,
    }


def reason_for(problem: str) -> Optional[Tuple[str, str]]:
    """Peppol's reason code and the status for one reason an invoice was not
    accepted, from `REASONS`; None for a reason that is not the supplier's."""
    for phrase, code, status in REASONS:
        if phrase in problem:
            return code, status
    return None


def response_xml(invoice: dict, identifier: str, code: str, when: datetime.datetime,
                 problems: Tuple[str, ...] = ()) -> bytes:
    """A Peppol Invoice Response to one invoice: a UBL `ApplicationResponse`
    from its buyer to its seller, saying `code`, and for each problem a reason
    from Peppol's list with the problem in words, then what the seller is
    asked to do."""
    def element(name: str, text: str, **attributes: str) -> str:
        return "<%s%s>%s</%s>" % (name, "".join(
            " %s=%s" % (key, quoteattr(value)) for key, value in attributes.items() if value),
            escape(text), name)

    def party(name: str, who: dict) -> str:
        return "<cac:%s>%s<cac:PartyLegalEntity>%s</cac:PartyLegalEntity></cac:%s>" % (
            name, element("cbc:EndpointID", who["endpoint"], schemeID=who["scheme"]),
            element("cbc:RegistrationName", who["name"]), name)

    statuses = "".join(
        "<cac:Status>%s%s</cac:Status>" % (
            element("cbc:StatusReasonCode", (reason_for(problem) or ("OTH",))[0],
                    listID="OPStatusReason"),
            element("cbc:StatusReason", problem))
        for problem in problems)
    if code in ACTIONS:
        statuses += "<cac:Status>%s</cac:Status>" % element(
            "cbc:StatusReasonCode", ACTIONS[code], listID="OPStatusAction")
    profile = BILLING_WITH_RESPONSE if invoice["profile"] == BILLING_WITH_RESPONSE else ANSWERING
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<ApplicationResponse xmlns=\"%s\" xmlns:cac=\"%s\" xmlns:cbc=\"%s\">"
            % (RESPONSE_NS, CAC.strip("{}"), CBC.strip("{}"))
            + element("cbc:CustomizationID", RESPONSE) + element("cbc:ProfileID", profile)
            + element("cbc:ID", identifier)
            + element("cbc:IssueDate", when.date().isoformat())
            + element("cbc:IssueTime", when.strftime("%H:%M:%S"))
            + party("SenderParty", invoice["buyer"]) + party("ReceiverParty", invoice["seller"])
            + "<cac:DocumentResponse><cac:Response>"
            + element("cbc:ResponseCode", code)
            + element("cbc:EffectiveDate", when.date().isoformat()) + statuses
            + "</cac:Response><cac:DocumentReference>"
            + element("cbc:ID", invoice["number"]) + element("cbc:IssueDate", invoice["issued"])
            + element("cbc:DocumentTypeCode", invoice["type"])
            + "</cac:DocumentReference></cac:DocumentResponse></ApplicationResponse>\n"
            ).encode("utf-8")


class EInvoices:
    """A supplier's e-invoices, collected, matched, posted and answered.

    `check` is the `InvoiceCheck` that does the matching and the posting;
    `supplier` is the base URL of the supplier's e-invoicing side. `now` is
    the clock the responses are dated by.

    `run` collects and matches, and says received, accepted, rejected or under
    query. `paid` says paid for what SAP has cleared since. Both return what
    they did, one dict per invoice.
    """

    def __init__(self, check: invoice_check.InvoiceCheck, supplier: str, now=None):
        self.check, self.sap, self.supplier = check, check.sap, supplier
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.taken = set()          # the supplier's ids of what was collected
        # invoice number -> what is known of it: the invoice as read, whether
        # it can be answered, what has been said, and the payable it became.
        self.known: Dict[str, dict] = {}
        self.responses = 0

    # -- the supplier -------------------------------------------------------------------

    def ask(self, method: str, path: str, body: Optional[bytes] = None) -> bytes:
        request = urllib.request.Request(self.supplier + path, data=body, method=method,
                                         headers={"Content-Type": "application/xml"})
        with urllib.request.urlopen(request) as response:
            return response.read()

    def collect(self) -> List[dict]:
        """What the supplier has sent that has not been taken yet: a result
        for each document left alone, and the invoices into `known`."""
        left = []
        for sent in json.loads(self.ask("GET", "/_mock/sent")):
            if sent["id"] in self.taken:
                continue
            self.taken.add(sent["id"])
            result = {"invoice": sent["number"], "po": ""}
            if sent["kind"] != "Invoice":
                left.append(dict(result, status="left", problems=[
                    "%s %s is not an invoice, and only invoices are checked"
                    % (sent["kind"], sent["number"])]))
                continue
            invoice = read_ubl(self.ask("GET", "/_mock/sent/%s/document" % sent["id"]))
            if invoice["number"] in self.known:
                # The same number again. It is matched like any other, and the
                # check says it is a duplicate; but the supplier's side finds
                # an invoice by its number, so what is said goes to the one
                # that is known, and nothing more is said here.
                left.append(dict(result, po=invoice["po"], status="left", problems=[
                    "invoice %s was collected before; this copy was not checked"
                    % invoice["number"]]))
                continue
            self.known[invoice["number"]] = {
                "invoice": invoice, "answerable": sent["specification"] == "peppol",
                "said": [], "accounting_document": "", "waiting": True}
            self.say(invoice["number"], "AB")
        return left

    def say(self, number: str, code: str, problems: Tuple[str, ...] = ()) -> None:
        """Tell the supplier, unless the invoice is one that has no response
        or the guide's order forbids it after what was said before."""
        known = self.known[number]
        if not known["answerable"] or self.final(known["said"]):
            return
        self.responses += 1
        self.ask("POST", "/responses", response_xml(
            known["invoice"], "%s-%d" % (number, self.responses), code, self.now(), problems))
        known["said"].append(code)

    @staticmethod
    def final(said: List[str]) -> bool:
        return bool(said) and said[-1] in ("RE", "PD")

    # -- matching and posting -----------------------------------------------------------

    def run(self) -> List[dict]:
        """Collect, match, post or block, and tell the supplier which.

        A response the supplier's side refuses is an `HTTPError`, raised to
        the caller: it means the response was wrong, and it is not said again.
        """
        results = self.collect()
        waiting = [known for known in self.known.values() if known["waiting"]]
        decided = []
        for known in waiting:
            invoice = known["invoice"]
            # What cannot be matched at all never reaches the check: an
            # invoice that names no order, or a line that names no order line.
            unreadable = list(invoice["unreadable"])
            if not invoice["po"]:
                unreadable.insert(0, "invoice %s names no purchase order" % invoice["number"])
            if unreadable:
                result = {"invoice": invoice["number"], "po": invoice["po"],
                          "status": "blocked", "problems": unreadable}
                results.append(result)
                decided.append((known, result))
            elif not any(kept is invoice for kept, _unanswered in self.check.pending):
                self.check.pending.append((invoice, False))
        # `InvoiceCheck.run` returns a result for each invoice it had pending,
        # in order, and then for any it collected itself.
        pending = list(self.check.pending)
        checked = self.check.run()
        result_of = {id(invoice): result
                     for (invoice, _unanswered), result in zip(pending, checked)}
        for known in waiting:
            result = result_of.get(id(known["invoice"]))
            if result is not None and result["status"] != "waiting":
                decided.append((known, result))
        results += checked
        for known, result in decided:
            known["waiting"] = False
            known["accounting_document"] = result.get("accounting_document", "")
            number = known["invoice"]["number"]
            told = tuple(p for p in result["problems"] if reason_for(p))
            if result["status"] == "posted":
                self.say(number, "AP")
            elif result["status"] == "blocked" and told:
                # Rejected if any reason is one only a new invoice cures.
                rejected = any(reason_for(p)[1] == "RE" for p in told)
                self.say(number, "RE" if rejected else "UQ", told)
            result["said"] = list(known["said"])
        return results

    # -- paid ---------------------------------------------------------------------------

    def cleared(self, accounting_document: str) -> bool:
        """Whether the payable an invoice became is cleared in SAP: it has a
        supplier item, and none of them is open."""
        query = urllib.parse.urlencode({"$filter": (
            "AccountingDocument eq '%s' and AccountingDocumentItemType eq 'K'"
            % accounting_document), "$format": "json"})
        rows = self.sap.request("GET", "%s?%s" % (ITEMS, query))["d"]["results"]
        return bool(rows) and all(row["ClearingAccountingDocument"] for row in rows)

    def paid(self) -> List[dict]:
        """Say paid for every invoice posted here that SAP has cleared since."""
        told = []
        for number, known in self.known.items():
            if not known["accounting_document"] or known.get("paid"):
                continue
            if self.cleared(known["accounting_document"]):
                known["paid"] = True
                self.say(number, "PD")
                told.append({"invoice": number, "status": "paid", "said": list(known["said"])})
        return told


__all__ = ["ACTIONS", "EInvoices", "NotAnInvoice", "REASONS", "read_ubl", "reason_for",
           "response_xml"]
