"""invoice_check: verify a supplier's EDI invoices against SAP before posting.

The three-way match every accounts-payable integration does: an 810 invoice
is posted into SAP (as an inbound INVOIC IDoc) only if

    - its prices agree with the purchase order in SAP,
    - it bills no more than the purchase order asked for, counting what
      earlier invoices for the same order have already billed,
    - it bills no more than the supplier's 856 ship notice said was shipped,
      and where the invoice names its shipment, that shipment's notice,
      counting what earlier invoices have already billed against that shipment,
    - its total adds up, tax included, and
    - it has not been posted before.

**Tax is read, not checked.** A supplier that charges sales tax sends it in
`TXI` segments, and the invoice total is then the lines plus the tax. The match
adds the tax in before comparing, and SAP is told the net, the tax and the
gross separately. Whether the *rate* is right is not checked: nothing here
knows what it should be. Charges and allowances (`SAC`) are not read at all, so
an invoice that carries one is blocked because it does not add up, and the
reason says so in those words.

An IDoc SAP accepted is not an invoice SAP posted, so the status record that
comes back decides: only status 53 counts as posted.

Anything else is blocked with the reasons, for a person to look at.

**An invoice ahead of its ship notice is held, not blocked and not posted.**
A supplier's 810 can name the shipment it bills (`REF*SI`), which is the
number its 856 carries (`BSN02`). Such an invoice is matched against that
consignment and no other, so a backorder's invoice is not let through on the
strength of the first delivery. If that 856 has not arrived the invoice is
`held`: kept, reported on every run, and posted on the run after the notice
comes. Nothing reaches SAP in between, so no payment run can pay for goods
nobody has said were sent. Anything else wrong with it blocks it at once: a
wrong price does not get better when the goods arrive. An invoice that names
no shipment is matched against the order's latest notice, as before, and is
blocked if it bills more.

**A shipment is received in SAP.** For each shipment its ship notice numbers,
a goods receipt is posted to SAP (`receive`): a material document with the
shipment's number as the supplier's delivery note, against the order's items,
material and plant, for the quantities received. SAP is asked first, so a
notice read twice, or by a process started again, is not received twice. By
default the notice is taken as the receipt, as soon as it is read: nothing
here models a warehouse counting what came off the truck. A caller that does
passes `receive_on_notice=False` and calls `receive` with what was counted.
The receipt posts no accounting document, in mock-sap and so here.

**An invoice that names its shipment is matched against its goods receipt.**
It is sent to SAP quoting the shipment as its delivery note (`E1EDP02` `QUALF`
`016`), and SAP keeps the receipt on each invoice item it billed. Until SAP
holds that receipt the invoice is held, as it is before its notice comes: SAP
would refuse an invoice quoting a receipt it does not have.

**A shipment is billed once, under however many numbers.** An invoice that
names its shipment is counted against it, so a second invoice for a shipment
already billed in full is blocked whatever number it carries, and two that
share a shipment between them are both posted. Without that the second one
passes every other check while the order still has quantity left: it bills
less than the shipment received, and no more than was ordered. Here the count
is this process's memory; `DurableInvoiceCheck` asks SAP what has been billed
against the shipment's receipt, which a restart does not lose. An invoice
that names no shipment cannot be counted this way, and is not.

**No over-delivery tolerance is read.** A real purchase order item can allow
some, as a percentage. mock-sap's item carries no such field, so none is
assumed: one unit over the order is blocked.

**An invoice is kept until SAP has dealt with it.** Collecting from the
supplier's mailbox takes a document out of it. If SAP then cannot be asked
about the order, or does not take the IDoc, the invoice stays in `pending` and
the next `run` tries again, rather than the document being gone. That is one
process's memory, as `posted` and `shipped` are: a restart loses it.

The supplier is mock-edi (https://github.com/rseufert/mock-edi), which answers
an 850 with a 997, an 855, an 856 and an 810, and misbehaves on request.  The
tests are in tests/test_invoice_check.py.  po_bridge.py, beside this file, is the
other half of the same integration: purchase orders out, confirmations in.
"""
import datetime
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from .po_bridge import x12_quantity

PO_SERVICE = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
MATERIAL_DOCUMENTS = ("/sap/opu/odata/sap/API_MATERIAL_DOCUMENT_SRV"
                      "/A_MaterialDocumentHeader")

# The longest delivery note a goods receipt holds (`MKPF-XBLNR`). mock-sap cuts
# a longer one short and keeps it, and an invoice quoting the whole number
# would then find no receipt; so a longer shipment number is not received.
DELIVERY_NOTE_LENGTH = 16
GOODS_RECEIPT = "101"       # the movement type of a receipt against an order

# SAP material -> the supplier's part number (the purchasing info record)
SUPPLIER_PART = {"TG11": "WIDGET-001", "TG12": "BRKT-050", "TG14": "GEAR-100"}

# The supplier's net payment days -> SAP's terms key. Every EDI integration
# configures a table like this, because the partner sends terms as numbers in an
# ITD segment and SAP wants the key that names them. Nothing is guessed: a net
# figure with no key is sent with no terms, which SAP reads as payable at once,
# rather than being rounded to whichever key is closest.
TERMS_BY_NET_DAYS = {0: "0001", 30: "NT30", 45: "NT45", 60: "NT60"}


class Sap:
    """Just enough of an OData/IDoc client: a session and a CSRF token."""

    def __init__(self, base):
        self.base = base
        jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.token = None

    def request(self, method, path, body=None, content_type="application/json"):
        headers = {"Accept": "application/json"}
        if method != "GET":
            if self.token is None:
                self.token = self._fetch_token()
            headers["X-CSRF-Token"] = self.token
            headers["Content-Type"] = content_type
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, headers, method=method)
        with self.http.open(req) as response:
            return json.loads(response.read())

    def _fetch_token(self):
        req = urllib.request.Request(self.base + PO_SERVICE + "/",
                                     headers={"X-CSRF-Token": "Fetch"})
        with self.http.open(req) as response:
            return response.headers["X-CSRF-Token"]

    def purchase_order(self, number):
        path = "%s/A_PurchaseOrder('%s')?$expand=to_PurchaseOrderItem" % (PO_SERVICE, number)
        return self.request("GET", path)["d"]


def edi(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=body.encode() if body else None,
                                 headers={"Content-Type": "application/edi-x12"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


def send_order(sap, edi_base, po_number, sender, receiver="MOCKEDI", control=1):
    """Send an SAP purchase order to the supplier as an 850.

    The minimum needed to set up an invoice; po_bridge.py does this
    properly and brings the 855 back.
    """
    po = sap.purchase_order(po_number)
    items = po["to_PurchaseOrderItem"]["results"]
    now = datetime.datetime.now(datetime.timezone.utc)
    body = ["ST*850*0001", "BEG*00*SA*%s**%s" % (po_number, now.strftime("%Y%m%d"))]
    # CUR, or the supplier bills in whatever its own default is - USD, for
    # mock-edi - and an order placed in EUR comes back invoiced in dollars. The
    # amounts then match number for number and mean different things, which is
    # what `problems` checks for below.
    body += ["CUR*BY*%s" % (po.get("DocumentCurrency") or "USD")]
    body += ["PO1*%s*%s*EA*%s**VP*%s" % (i["PurchaseOrderItem"],
                                          x12_quantity(i["OrderQuantity"]),
                                          i["NetPriceAmount"], SUPPLIER_PART[i["Material"]])
             for i in items]
    body += ["CTT*%d" % len(items)]
    body += ["SE*%d*0001" % (len(body) + 1)]
    envelope = [
        "ISA*00*%-10s*00*%-10s*ZZ*%-15s*ZZ*%-15s*%s*%s*U*00401*%09d*0*T*>" % (
            "", "", sender, receiver, now.strftime("%y%m%d"), now.strftime("%H%M"), control),
        "GS*PO*%s*%s*%s*%s*%d*X*004010" % (
            sender, receiver, now.strftime("%Y%m%d"), now.strftime("%H%M"), control)]
    trailer = ["GE*1*%d" % control, "IEA*1*%09d" % control]
    return edi(edi_base, "POST", "/edi", "~\n".join(envelope + body + trailer) + "~\n")


def odata_string(value):
    """A string for an OData `$filter` literal, with its quotes doubled.

    A supplier's invoice number is the supplier's, not ours. `O'BRIEN-014` would
    close the literal early and the filter becomes a different question - or a
    syntax error, if you are lucky enough to notice. OData escapes a quote by
    doubling it, and `urlencode` then percent-encodes the result as the value it
    is rather than as syntax.
    """
    return str(value).replace("'", "''")


def segments(payload):
    for segment in payload.replace("\n", "").split("~"):
        if segment:
            yield segment.split("*")


def read_856(payload):
    """The ship notice as (po_number, {sap_item: quantity shipped})."""
    po_number, shipped = "", {}
    for fields in segments(payload):
        if fields[0] == "PRF":
            po_number = fields[1]
        elif fields[0] == "SN1":
            shipped[fields[1]] = Decimal(fields[2])
    return po_number, shipped


def shipment_number(payload):
    """The number a ship notice gives its consignment (`BSN02`), or ""."""
    for fields in segments(payload):
        if fields[0] == "BSN" and len(fields) > 2:
            return fields[2]
    return ""


def read_810(payload):
    """The invoice as a dict: number, po, date, currency, terms, lines, tax, total.

    And ``shipment``: the consignment the invoice says it bills (``REF*SI``),
    or "" where it names none.

    The three-way match needs only the numbers, but posting the invoice needs
    what it was billed *on*: ``BIG01`` is the invoice date, which becomes the
    payable's baseline and so decides when a payment run picks it up, and the
    net days in ``ITD`` decide the terms. Reading only the match's fields is how
    this example came to post invoices that owed nobody anything.

    ``tax`` is every ``TXI02`` added up: the tax amounts, whichever tax each is
    and whether it stands in the summary or under a line. ``TDS01`` includes
    them, so a reader that skips ``TXI`` sees a total its lines do not reach.
    """
    invoice = {"lines": {}, "date": "", "currency": "", "net_days": None,
               "tax": Decimal("0.00"), "shipment": ""}
    for fields in segments(payload):
        if fields[0] == "BIG":
            invoice["date"] = fields[1]
            invoice["number"], invoice["po"] = fields[2], fields[4]
        elif fields[0] == "CUR":
            invoice["currency"] = fields[2] if len(fields) > 2 else ""
        elif fields[0] == "REF" and len(fields) > 2 and fields[1] == "SI":
            invoice["shipment"] = fields[2]
        elif fields[0] == "ITD":
            # ITD07 is the net days; an ITD that does not carry them leaves the
            # invoice on no terms rather than on invented ones.
            net = fields[7] if len(fields) > 7 else ""
            invoice["net_days"] = int(net) if net.strip().isdigit() else None
        elif fields[0] == "IT1":
            invoice["lines"][fields[1]] = (Decimal(fields[2]), Decimal(fields[4]))
        elif fields[0] == "TXI":
            if len(fields) > 2 and fields[2].strip():
                invoice["tax"] += Decimal(fields[2])
        elif fields[0] == "TDS":
            invoice["total"] = Decimal(fields[1]) / 100     # two implied decimals
    return invoice


def invoic_idoc(invoice, po):
    """The invoice as the INVOIC02 IDoc SAP's invoice verification reads.

    Identifying the document is not enough to post it. SAP needs to know who to
    owe and what for, and the segments it reads for that are:

      - ``E1EDKA1`` with ``PARVW`` ``LF``, the supplier who billed us. Without
        it there is nobody to owe, so nothing is created - and mock-sap answered
        status 53 anyway until 0.13.2, which is how every invoice this example
        ever "posted" came to leave no payable behind (mock-sap#67, mock-sap#68).
      - ``E1EDK02`` with ``QUALF`` **009**, the supplier's own invoice number.
        This is what ``SupplierInvoiceIDByInvcgParty`` is set from, and what a
        payment run puts in the payment's ``EndToEndId`` so the bank's answer
        can be matched back. ``QUALF`` ``001``, which is all this used to send,
        is the purchase order: a different reference for a different purpose.
        mock-sap will fall back to ``E1EDK01``/``BELNR`` when ``009`` is absent,
        and this sends both, so removing ``009`` changes nothing here - but the
        fallback is this mock's kindness, not something to rely on against a
        real system, where ``009`` is where an invoicing party's reference goes.
      - ``E1EDK03`` with ``IDDAT`` ``026``, the invoice date, which becomes the
        payable's baseline date and so decides when it falls due.
      - ``NETWR`` and ``VGBEL``/``VGPOS`` per item, or the payable's lines carry
        no value and name no purchase order.
      - ``E1EDP02`` with ``QUALF`` **016** per item, where the invoice names its
        shipment: the supplier's delivery note, by which SAP finds the goods
        receipt the item bills and keeps it on the item (mock-sap#193).
      - ``E1EDS01`` sums. ``010`` is what is owed, tax included. When the
        invoice carries tax, ``011`` is the net and ``205`` the tax, so that SAP
        books the expense and the input tax apart instead of expensing the tax.
        Those three qualifiers are the ones mock-sap reads; they were not
        checked against a real system's partner profile.

    The supplier number comes from the purchase order in SAP, not from the 810:
    the invoice names the supplier by their EDI id (``N1*RE*...*92*MOCKEDI``),
    and SAP owes money to a vendor number.
    """
    supplier = po["Supplier"]
    currency = invoice.get("currency") or po.get("DocumentCurrency") or "USD"
    terms = TERMS_BY_NET_DAYS.get(invoice.get("net_days"), "")
    tax = invoice.get("tax") or Decimal("0.00")
    note = ("<E1EDP02><QUALF>016</QUALF><BELNR>%s</BELNR></E1EDP02>" % invoice["shipment"]
            if invoice.get("shipment") else "")
    sums = "<E1EDS01><SUMID>010</SUMID><SUMME>%s</SUMME></E1EDS01>" % invoice["total"]
    if tax:
        sums += ("<E1EDS01><SUMID>011</SUMID><SUMME>%s</SUMME></E1EDS01>"
                 "<E1EDS01><SUMID>205</SUMID><SUMME>%s</SUMME></E1EDS01>"
                 % (invoice["total"] - tax, tax))
    items = "".join(
        "<E1EDP01><POSEX>%s</POSEX><MENGE>%s</MENGE><VPREI>%s</VPREI>"
        "<NETWR>%s</NETWR><VGBEL>%s</VGBEL><VGPOS>%s</VGPOS>"
        "<E1EDP02><QUALF>001</QUALF><BELNR>%s</BELNR><ZEILE>%s</ZEILE></E1EDP02>%s</E1EDP01>"
        % (item, qty, price, qty * price, invoice["po"], item, invoice["po"], item, note)
        for item, (qty, price) in sorted(invoice["lines"].items()))
    return ("<INVOIC02><IDOC BEGIN=\"1\"><EDI_DC40 SEGMENT=\"1\">"
            "<IDOCTYP>INVOIC02</IDOCTYP><MESTYP>INVOIC</MESTYP><DIRECT>2</DIRECT>"
            "</EDI_DC40><E1EDK01 SEGMENT=\"1\"><BELNR>%s</BELNR>"
            "<CURCY>%s</CURCY><ZTERM>%s</ZTERM><BSART>INVO</BSART></E1EDK01>"
            "<E1EDK02><QUALF>001</QUALF><BELNR>%s</BELNR></E1EDK02>"
            "<E1EDK02><QUALF>009</QUALF><BELNR>%s</BELNR></E1EDK02>"
            "<E1EDK03><IDDAT>026</IDDAT><DATUM>%s</DATUM></E1EDK03>"
            "<E1EDKA1><PARVW>LF</PARVW><PARTN>%s</PARTN><LIFNR>%s</LIFNR></E1EDKA1>%s"
            "%s</IDOC></INVOIC02>"
            % (invoice["number"], currency, terms, invoice["po"], invoice["number"],
               invoice["date"], supplier, supplier, items, sums))


class InvoiceCheck:
    def __init__(self, sap, edi_base, our_id, receive_on_notice=True):
        self.sap, self.edi_base, self.our_id = sap, edi_base, our_id
        # Whether a ship notice is received in SAP as soon as it is read.
        self.receive_on_notice = receive_on_notice
        # Shipments to receive that SAP could not be asked about or did not
        # take: (po_number, shipment number, {item: quantity}), tried again
        # on the next run.
        self.unreceived = []
        # What became of each receipt: {"po", "shipment", and
        # "material_document" or "problem"}, oldest first.
        self.receipts = []
        self.shipped = {}       # po_number -> {item: quantity}, from 856s
        # (po_number, shipment number) -> {item: quantity}: each 856 by the
        # consignment it advises, for an invoice that names the one it bills.
        self.notices = {}
        self.posted = set()     # invoice numbers already in SAP
        self.billed = {}        # po_number -> {item: quantity}, from what was posted
        # (po_number, shipment number) -> {item: quantity}, from what was
        # posted for an invoice that named its shipment.
        self.billed_for = {}
        # 810s collected and not yet dealt with: (invoice, whether an earlier
        # run posted its IDoc and SAP never answered).
        self.pending = []

    def already_billed(self, po_number, item):
        """What earlier invoices have billed for this order item.

        From this object's own memory of what it posted, which is all it has.
        A check that must survive a restart asks SAP instead, as
        `procure_to_pay.DurableInvoiceCheck` does.
        """
        return self.billed.get(po_number, {}).get(item, Decimal(0))

    def already_billed_for(self, po_number, shipment, item):
        """What earlier invoices have billed for this item of this shipment.

        From this object's own memory of what it posted. SAP can say too, by
        the goods receipt each invoice item billed, and
        `procure_to_pay.DurableInvoiceCheck` asks it.
        """
        return self.billed_for.get((po_number, shipment), {}).get(item, Decimal(0))

    # -- goods receipts -----------------------------------------------------

    def receipt(self, po_number, shipment):
        """SAP's goods receipt for this shipment of this order, or None:
        `{"documents": [(material document, year)], "items": {item: quantity}}`.

        Found by the delivery note, which is the shipment's number, and kept
        to the items received against this order. Two receipts under one
        delivery note, which this check never posts, are added together.
        """
        query = urllib.parse.urlencode({
            "$filter": "ReferenceDocument eq '%s'" % odata_string(shipment),
            "$expand": "to_MaterialDocumentItem", "$format": "json"})
        found = {"documents": [], "items": {}}
        for header in self.sap.request("GET", "%s?%s" % (MATERIAL_DOCUMENTS, query))["d"]["results"]:
            lines = [line for line in header["to_MaterialDocumentItem"]["results"]
                     if line["PurchaseOrder"] == po_number]
            if not lines:
                continue
            found["documents"].append((header["MaterialDocument"],
                                       header["MaterialDocumentYear"]))
            for line in lines:
                item = line["PurchaseOrderItem"]
                found["items"][item] = (found["items"].get(item, Decimal(0))
                                        + Decimal(line["QuantityInEntryUnit"]))
        return found if found["documents"] else None

    def receive(self, po_number, shipment, lines, po=None):
        """Post SAP's goods receipt for a shipment, unless SAP has one.

        `lines` is `{item: quantity received}`, by the order's item numbers.
        Returns what became of it, as `receipts` keeps it. An item that is not
        on the order is not received: a receipt item names the order item it
        receives, and SAP has no such item. Raises what SAP's answer raises,
        for the caller to try again.
        """
        outcome = {"po": po_number, "shipment": shipment}
        if not shipment:
            return dict(outcome, problem="the ship notice gives the shipment no number "
                        "(BSN02), so there is no delivery note to receive it under")
        if len(shipment) > DELIVERY_NOTE_LENGTH:
            return dict(outcome, problem="shipment %s is longer than the %d characters "
                        "a goods receipt keeps of a delivery note, so it was not received"
                        % (shipment, DELIVERY_NOTE_LENGTH))
        existing = self.receipt(po_number, shipment)
        if existing:
            return dict(outcome, material_document=existing["documents"][0][0],
                        already=True)
        po = po or self.sap.purchase_order(po_number)
        ordered = {i["PurchaseOrderItem"]: i for i in po["to_PurchaseOrderItem"]["results"]}
        items = [{"Material": ordered[item]["Material"], "Plant": ordered[item]["Plant"],
                  "PurchaseOrder": po_number, "PurchaseOrderItem": item,
                  "GoodsMovementType": GOODS_RECEIPT,
                  "QuantityInEntryUnit": str(quantity),
                  "EntryUnit": ordered[item]["PurchaseOrderQuantityUnit"]}
                 for item, quantity in sorted(lines.items()) if item in ordered and quantity]
        if not items:
            return dict(outcome, problem="shipment %s received nothing that is on purchase "
                        "order %s, so it was not received" % (shipment, po_number))
        posted = self.sap.request("POST", MATERIAL_DOCUMENTS, {
            "ReferenceDocument": shipment, "to_MaterialDocumentItem": items})
        outcome["material_document"] = posted["d"]["MaterialDocument"]
        strays = sorted(set(lines) - set(ordered))
        if strays:
            outcome["problem"] = ("item %s of shipment %s is not on purchase order %s, "
                                  "so it was not received" % (", ".join(strays), shipment,
                                                              po_number))
        return outcome

    def receive_waiting(self):
        """Receive each shipment waiting to be, keeping what SAP did not take."""
        waiting, self.unreceived = self.unreceived, []
        for po_number, shipment, lines in waiting:
            try:
                self.receipts.append(self.receive(po_number, shipment, lines))
            except urllib.error.HTTPError as error:
                # A 5xx is SAP saying it could not, and the receipt is tried
                # again; anything else is SAP saying no, which trying again
                # does not change.
                again = error.code >= 500
                self.receipts.append({"po": po_number, "shipment": shipment, "problem":
                                      "SAP answered %d to its goods receipt, so it was %s"
                                      % (error.code, "kept for the next run" if again
                                         else "not received")})
                if again:
                    self.unreceived.append((po_number, shipment, lines))
            except (urllib.error.URLError, OSError) as error:
                self.receipts.append({"po": po_number, "shipment": shipment, "problem":
                                      "SAP did not answer about its goods receipt, so it "
                                      "was kept for the next run: %s"
                                      % getattr(error, "reason", error)})
                self.unreceived.append((po_number, shipment, lines))

    def after_no_answer(self, invoice, po):
        """Why an invoice whose IDoc SAP never answered must not be sent again.

        A request that got no answer may have arrived. SAP does not refuse a
        second copy of an invoice, so sending it again could leave two payables
        for one bill, and this check has no way to ask which happened. It says
        so and leaves it to a person. A check that can ask SAP returns `[]`.
        """
        return ["SAP did not answer when invoice %s was sent on an earlier run, so "
                "it may hold it already; it was not sent again" % invoice["number"]]

    def awaited(self, invoice):
        """Why an invoice naming its shipment waits, or "": its ship notice has
        not arrived, or SAP holds no goods receipt for the shipment yet."""
        named = invoice.get("shipment") or ""
        if not named or self.receipt(invoice["po"], named):
            return ""
        if (invoice["po"], named) in self.notices:
            return ("its ship notice has arrived, and SAP holds no goods receipt for "
                    "that shipment")
        return "no ship notice for that shipment has arrived"

    def shipped_for(self, invoice):
        """What this invoice is matched against: for the shipment it names,
        what SAP's goods receipt for it received; where it names none, what
        the order's latest ship notice says was sent. None while the shipment
        it names has no receipt, which is not a quantity to compare with."""
        named = invoice.get("shipment") or ""
        if not named:
            return self.shipped.get(invoice["po"], {})
        receipt = self.receipt(invoice["po"], named)
        return receipt["items"] if receipt else None

    def problems(self, invoice, po):
        """Why this invoice must not be posted; empty if it may be."""
        if invoice["number"] in self.posted:
            return ["invoice %s has already been posted" % invoice["number"]]
        ordered = {i["PurchaseOrderItem"]: i for i in po["to_PurchaseOrderItem"]["results"]}
        shipped = self.shipped_for(invoice)
        found, total = [], Decimal(0)
        # Before any amount is compared: the same number in another currency is
        # not the same price. Every check below subtracts and compares bare
        # decimals, so a USD invoice against a EUR order would pass them all.
        billed = invoice.get("currency") or ""
        ordered_in = po.get("DocumentCurrency") or ""
        if billed and ordered_in and billed != ordered_in:
            found.append("invoice is in %s, purchase order %s is in %s"
                         % (billed, invoice["po"], ordered_in))
        for item, (qty, price) in sorted(invoice["lines"].items()):
            total += qty * price
            if item not in ordered:
                found.append("item %s is not on purchase order %s" % (item, invoice["po"]))
                continue
            po_price = Decimal(ordered[item]["NetPriceAmount"])
            if price != po_price:
                found.append("item %s billed at %s, ordered at %s" % (item, price, po_price))
            # Against the order, and not only against the ship notice: the 856
            # is the supplier's own account of what it sent, so a supplier that
            # ships too much and bills it agrees with itself (#12).
            asked_for = Decimal(ordered[item]["OrderQuantity"])
            before = self.already_billed(invoice["po"], item)
            if before + qty > asked_for:
                found.append("item %s bills %s%s, ordered %s" % (
                    item, qty, ", with %s already billed" % x12_quantity(before)
                    if before else "", x12_quantity(asked_for)))
            if shipped is not None:
                # Against the shipment, and not only against its notice: a
                # second invoice for a shipment already billed is for less
                # than was sent, and for goods already billed (#41).
                named = invoice.get("shipment") or ""
                earlier = self.already_billed_for(invoice["po"], named, item)
                if earlier + qty > shipped.get(item, 0):
                    found.append("item %s bills %s, %s %s%s" % (
                        item, qty, "received" if named else "shipped",
                        x12_quantity(shipped.get(item, Decimal(0))),
                        " in shipment %s%s" % (named, ", with %s already billed against it"
                                               % x12_quantity(earlier) if earlier else "")
                        if named else ""))
        tax = invoice.get("tax") or Decimal("0.00")
        if total + tax != invoice["total"]:
            # Said with the tax, or without it when there is none, so that the
            # reason reads the same as it always did for an untaxed invoice.
            found.append("lines add up to %s%s, invoice total is %s" % (
                total, " and tax to %s" % tax if tax else "", invoice["total"]))
        return found

    def run(self):
        """Collect ship notices and invoices; post what matches, block the rest.

        Collecting from the mailbox takes a document out of it, so only the two
        kinds this reads are asked for. Asking for the whole mailbox took the
        supplier's order responses as well and dropped them, and `po_bridge`,
        reading the same mailbox afterwards, never confirmed the order (#8).
        """
        docs = [d for kind in ("despatch", "invoice")
                for d in edi(self.edi_base, "GET", "/_mock/mailbox?partner=%s&kind=%s"
                             % (self.our_id, kind))
                if d["code"] in ("856", "810")]
        for doc in docs:                       # ship notices first: invoices need them
            if doc["code"] == "856":
                po_number, lines = read_856(doc["payload"])
                self.shipped.setdefault(po_number, {}).update(lines)
                number = shipment_number(doc["payload"])
                if number:
                    self.notices[(po_number, number)] = lines
                if self.receive_on_notice:
                    self.unreceived.append((po_number, number, lines))
        # Before the invoices, which wait for their shipment's receipt.
        self.receive_waiting()
        # Kept before anything is asked of SAP, so that an invoice SAP cannot
        # be asked about is still here next time (#13).
        self.pending += [(read_810(doc["payload"]), False)
                         for doc in docs if doc["code"] == "810"]
        results, still_pending = [], []
        for invoice, unanswered in self.pending:
            result, keep = self.check_one(invoice, unanswered)
            results.append(result)
            if keep is not None:
                still_pending.append((invoice, keep))
        self.pending = still_pending
        return results

    def check_one(self, invoice, unanswered):
        """One invoice: matched, and posted or blocked, or left for the next run.

        Left for the next run is `waiting`, where SAP could not be asked or
        did not answer, or `held`, where its ship notice has not arrived.

        Returns the result and what to keep: `None` when the invoice is dealt
        with, otherwise whether its IDoc has been sent with no answer.
        """
        result = {"invoice": invoice["number"], "po": invoice["po"]}

        def waiting(what, error):
            result.update(status="waiting", problems=[
                "%s, so invoice %s was kept for the next run: %s"
                % (what, invoice["number"], getattr(error, "reason", error))])
            return result

        # One read of the order, for the match and for the IDoc: the supplier
        # to owe is on the order, not on the invoice.
        try:
            po = self.sap.purchase_order(invoice["po"])
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                return waiting("SAP answered %d to reading purchase order %s"
                               % (error.code, invoice["po"]), error), unanswered
            # SAP answered, and the answer is that it has no such order, or will
            # not show it. Trying again changes nothing; a person has to look.
            result.update(status="blocked", problems=[
                "SAP answered %d to reading purchase order %s"
                % (error.code, invoice["po"])])
            return result, None
        except (urllib.error.URLError, OSError) as error:
            return waiting("SAP did not answer about purchase order %s"
                           % invoice["po"], error), unanswered

        result["problems"] = ((self.after_no_answer(invoice, po) if unanswered else [])
                              or self.problems(invoice, po))
        if result["problems"]:
            result["status"] = "blocked"
            return result, None
        awaited = self.awaited(invoice)
        if awaited:
            # Nothing else is wrong with it, and nothing says the goods were
            # sent. Not posted, so nothing can pay it; kept, so that it posts
            # when the notice comes.
            result.update(status="held", problems=[
                "invoice %s bills shipment %s, and %s, so it was kept for the next run"
                % (invoice["number"], invoice["shipment"], awaited)])
            return result, unanswered
        try:
            receipt = self.sap.request("POST", "/sap/bc/idoc",
                                       invoic_idoc(invoice, po), "application/xml")
        except urllib.error.HTTPError as error:
            if error.code >= 500:
                # SAP answered that it could not, so it holds nothing and the
                # same IDoc can be sent again.
                return waiting("SAP answered %d to the invoice IDoc" % error.code,
                               error), False
            result.update(status="not posted", problems=[
                "SAP answered %d to the invoice IDoc" % error.code])
            return result, None
        except (urllib.error.URLError, OSError) as error:
            # No answer at all: it may have arrived. Kept, and marked, so that
            # the next run asks `after_no_answer` before it sends anything.
            return waiting("SAP did not answer the invoice IDoc", error), True
        # A 201 means SAP took the IDoc, not that it posted the invoice.
        # The status record says which, and only 53 is posted; treating
        # the docnum as success books an invoice SAP rejected, and marks
        # the number as posted so the resend looks like a duplicate.
        if receipt.get("STATUS") == "53":
            self.posted.add(invoice["number"])
            billed = self.billed.setdefault(invoice["po"], {})
            for item, (qty, _) in invoice["lines"].items():
                billed[item] = billed.get(item, Decimal(0)) + qty
            if invoice.get("shipment"):
                # Against its shipment as well, where it names one.
                billed_for = self.billed_for.setdefault(
                    (invoice["po"], invoice["shipment"]), {})
                for item, (qty, _) in invoice["lines"].items():
                    billed_for[item] = billed_for.get(item, Decimal(0)) + qty
            result.update(status="posted", idoc=receipt["DOCNUM"])
            # What posting it actually created. An INVOIC that posts
            # leaves a supplier invoice and money owed; saying so here
            # is what makes "posted" mean something a payment run can
            # find, rather than only that SAP took the file.
            applied = (receipt.get("APPLIED") or [{}])[0]
            result.update(supplier_invoice=applied.get("SUPPLIERINVOICE", ""),
                          accounting_document=applied.get("ACCOUNTINGDOCUMENT", ""))
        else:
            result.update(status="not posted", idoc=receipt["DOCNUM"],
                          problems=["IDoc %s is in status %s: %s"
                                    % (receipt["DOCNUM"], receipt.get("STATUS"),
                                       receipt.get("STATUS_TEXT", ""))])
        return result, None
