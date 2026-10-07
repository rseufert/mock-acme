# Changelog

Every release of [mock-acme](https://pypi.org/project/mock-acme/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

### Added

- **`e_invoice`: a supplier's UBL e-invoice, matched, posted and answered**
  ([#34]). The invoice comes from mock-einvoice instead of as an 810; the
  order and the ship notice still go by EDI, with a supplier who sends no
  810. It is read into the dict `read_810` makes, so the three-way match, the
  INVOIC IDoc and the duplicate question are `InvoiceCheck`'s own. The
  supplier is then told with Peppol Invoice Responses: `AB` when the invoice
  is collected, `AP` when SAP posts it, `RE` or `UQ` with a reason from
  Peppol's list and the problem in words when it is blocked, and `PD` when
  the payable is cleared in SAP, which is what the remittance advice rests on
  too. A block that is not the supplier's doing is not told. An XRechnung
  invoice is matched and posted and told nothing, because XRechnung has no
  response message. The tests need **mock-einvoice 0.1.0**, a fourth mock,
  which `tests/__init__.py` now starts with the others.

## [0.3.2] - 2026-10-06

One fix to the payment run, in both formats: a payment file the bank refused
whole could leave its invoices held in SAP for a payment that did not exist,
with nothing reported. Nothing else changes.

### Fixed

- **A payment file the bank refuses whole is a refusal, whether or not the
  bank's report names the run** ([#30]). The bank writes its report to the
  file it read, and a file it could not read far enough has no name the run
  knows. No report was matched, so every item stayed `sent` and claimed in
  SAP with no problem reported: held for a payment that did not exist, and
  skipped by every later run. The refusal is in the bank's answer to the file
  as well, and the run now reads it there: the items are `rejected` with the
  bank's reason code, their claims in SAP come off, and the run reports a
  problem carrying what the bank said. A copy of a file the bank already has
  (`DUPL`) is still a copy, read from the same answer, and its items stay as
  the first file left them. `Run.refused` holds the code and the words.

## [0.3.1] - 2026-10-06

Two fixes to the payment run paying by ACH. In both, one bad value could cost
every payment in the file, or leave them waiting with nothing said; now the
one item is skipped with a reason, or the run is refused before it selects
anything. Nothing changes for a run paying by SEPA transfer.

**One thing a caller may notice:** a company routing number that is not nine
of the digits 0 to 9 now refuses the run, where the file used to go.

### Fixed

- **A control character in a name no longer costs every payment in an ACH
  file** ([#25]). In NACHA mode the run checked that a supplier's name, account
  number and reference, and the company's name and identification, were ASCII.
  A tab, a line feed or a DEL is ASCII and is not a character a NACHA record
  holds: a line feed ended the record in the middle of the name and the bank
  refused the whole file, and mock-bank 0.9.0 refuses the other two as
  well. Each is now held to printable ASCII (0x20 to 0x7E), so the one item
  is skipped, or the run refused if it is the company's, and the reason names
  the character.
- **A routing number that is not nine digits no longer reaches an ACH file**
  ([#27]). A supplier's was checked with `str.isdigit()`, which is true of
  digits that are not 0 to 9, and the bank refused the whole file. The
  company's was not checked at all: with eight digits or ten the file went,
  no acknowledgement named it, and its items stayed `sent` with no problem
  reported; with none the run raised `KeyError`. Now a supplier's skips that
  item and the rest are paid, and the company's refuses the run before
  anything is selected. The check digit is still left to the bank, which
  rejects that one entry.

## [0.3.0] - 2026-10-05

The payment run now leans on mock-sap 0.19.0 for two things it used to work
around. A run says in SAP which run has each invoice before its file goes, so
a second run does not pay it again whoever starts it; and money arriving is
posted with the reference it quotes, because SAP can now tell a receipt from a
return.

**`payment_run` needs mock-sap 0.19.0 or later.** Against an older one a run
pays nothing: SAP refuses the field the claim is written in, and each item is
skipped with that as its reason.

**Four behaviours change for a caller**, under Changed: read them before
upgrading from 0.2.0.

### Added

- **A run claims each item in SAP before it sends the file** ([#21]). It
  writes its identification and date on the invoice, as `PaymentRunID` and
  `PaymentRunDate`, which mock-sap carries to the open item from 0.19.0
  (rseufert/mock-sap#90). A run that finds another run's claim skips the item
  and names that run. SAP takes the claim off when a statement clears the item
  or a returned payment reopens it; the run takes it off when the bank refuses
  the payment. `Item` carries `run_id`, `run_date` and `invoice`.
- An item SAP would not take the claim for is skipped, not sent, and is a
  problem on the run.

### Changed

- **A run's identification is one to six characters**, SAP's limit for it
  ([#21]). Anything else is refused before an open item is selected. It was
  any string outside NACHA mode.
- **`Register` is optional and no longer needed** ([#21]). `PaymentRun(...)`
  and `ProcureToPay(...)` without one keep no record of their own, where they
  kept one in memory. A caller that passes one gets what it did before, beside
  the claim: asked after SAP, and let go of by this code. Its docstring says
  what a second record costs.
- The reason on an item another run holds reads `in payment: SAP has it with
  payment run R1 of 2026-10-05, ...`. It named the file's `MsgId`.
- **A run no longer reports money arriving as a problem** ([#18]). The problem
  said a reference had been withheld, and none is. What SAP made of the line
  is on the statement's record, under `unprocessed`, in SAP's own words.
- **Money arriving is posted to SAP with the reference it quotes** ([#18]).
  It was left off, so that a mock-sap that could not tell a receipt from a
  return did not reopen an invoice on it. Since 0.2.0 each credit says which
  it is, and mock-sap reads that from 0.19.0, so the reference is back: it is
  what SAP will clear a receivable by (rseufert/mock-sap#65).
- The test extra requires `mock-sap>=0.19.0`, from 0.17.0.

## [0.2.0] - 2026-10-05

A second payment run before the statement no longer pays an invoice again,
`invoice_check` is corrected in five places, and `procure_to_pay` ends with the
supplier told what was paid. The statement posted to SAP also says what the
next mock-sap release will need it to. **Two behaviours change for a caller**,
under Changed: read them before upgrading from 0.1.0.

### Added

- **`ProcureToPay.advise` tells the supplier what was paid** ([#15]). The loop
  ended with SAP and the bank agreeing and the supplier not told. `advise`
  takes a reconciled run, asks SAP for the payment advice of each payment it
  cleared, and sends each on as an X12 820 through `remittance`, one per
  payment document. It returns what the supplier made of each, disagreements
  included. A payment the bank refused, or one not yet on a statement, is not
  advised. A payment that later comes back is not corrected: no reversing 820
  is sent.
- **`ProcureToPay` takes the payment run's register** ([#2]).
  `ProcureToPay(..., register=Register(path))` hands it to the `PaymentRun` it
  builds. Without this the register kept in a file, which is the one a
  restarted middleware needs, could not be had through `ProcureToPay` at all.
  Left out, it is in memory as before.
- **A credit on the statement posted to SAP says which kind it is** ([#18]).
  `payment_run` writes `LINACTION` on each credit line of the `FINSTA01`: `RET`
  for a payment coming back, `RCV` for money arriving. mock-sap reads it from
  the release after 0.18.0, where a credit that declares neither reverses
  nothing, so a return that did not say so would no longer reopen its invoice.
  mock-sap up to 0.18.0 ignores the field, and this package works with both.

### Changed

- **A bank that does not answer leaves the run's items held under that run**
  ([#2]). The items stay `selected`, as before, and the same run can be sent
  again. A different run no longer pays them, because a request that timed
  out may have arrived.
- **`InvoiceCheck.run` reports a failure to reach SAP instead of raising**
  ([#13]). A result may now have the status `waiting`: SAP answered with a 5xx
  or did not answer, and the invoice is kept for the next run. An order SAP
  does not have is `blocked`. When SAP did not answer the invoice IDoc itself,
  the invoice may have arrived, so `InvoiceCheck` does not send it again and
  says so on the next run; `DurableInvoiceCheck` asks SAP whether it holds it
  and sends it again only if not.

### Fixed

- **A second payment run before the statement no longer pays the same invoice
  again** ([#2]). `payment_run` keeps a `Register` of what it has sent to the
  bank, written before the file goes out. A later run skips an item another
  run holds and says which run has it; the run that holds it may still send
  its own file again. An item is let go when the bank refuses the payment or
  SAP clears it from a statement, so a payment that comes back is open to the
  next run as before.
  `PaymentRun(..., register=Register(path))` keeps the register in a file.
  **Without `register` it is kept in memory**, which protects one
  `PaymentRun` object and no other process. It is the caller's own record, not
  SAP's: rseufert/mock-sap#90 is still the fix for two installations, or two
  processes at once.
- **A fractional quantity is ordered as it stands** ([#8]). `po_bridge` and
  `invoice_check.send_order` wrote a purchase order's quantity into the 850 with
  `%d`, so an order for 2.5 was sent as an order for 2. The supplier confirmed,
  shipped and billed 2, and the three-way match found nothing wrong.
- **An invoice that carries sales tax is no longer blocked for it** ([#8]).
  `invoice_check` reads the `TXI` segments, adds the tax in before comparing
  the total, and tells SAP the net, the tax and the gross apart. Against
  mock-edi started with `--tax-rate`, every correct invoice used to be blocked
  as not adding up. The rate itself is not checked, and charges and allowances
  (`SAC`) are still not read.
- **`po_bridge` and `invoice_check` no longer take each other's documents out
  of the supplier's mailbox** ([#8]). Each collected the whole mailbox and kept
  what it read, so whichever ran first dropped the other's: no confirmation
  reached SAP, or no invoice did. Each now asks only for the kinds it reads.
- **An invoice for more than was ordered is blocked** ([#12]). The three-way
  match compared what was billed with what the ship notice said was shipped,
  and never with the order, so a supplier that shipped and billed 150 against
  an order for 100 was posted in full. It now counts what earlier invoices for
  the same order item have billed as well. No over-delivery tolerance is read:
  mock-sap's order item carries none, so one unit over is blocked.
  `InvoiceCheck` counts from its own memory of what it posted;
  `procure_to_pay.DurableInvoiceCheck` asks SAP.
- **An invoice taken out of the supplier's mailbox is kept until SAP has dealt
  with it** ([#13]). If SAP could not be asked about the order, `run` raised
  and the collected invoices were gone. They now stay in `pending`, and the
  next run tries again.

## [0.1.0] - 2026-10-05

The first release.

### Added

- **The integrations that lived in the mocks' `examples/` folders, in one
  package** ([#1], [#4]). `mockacme.po_bridge` from mock-edi;
  `mockacme.invoice_check` and `mockacme.remittance` from mock-sap;
  `mockacme.pay_invoices`, `mockacme.payment_run`, `mockacme.procure_to_pay`
  and the `mockacme.bank_messages` they share from mock-bank. Each mock has
  removed its copy, starting with the release after mock-sap 0.17.1, mock-edi
  0.7.0 and mock-bank 0.7.0. Anything that imported
  `mockbank.examples.payment_run` imports `mockacme.payment_run` instead.
- **Tests that start the three mocks themselves** ([#1]). Importing `tests`
  starts mock-sap, mock-edi and mock-bank on ports the operating system chose,
  so there is nothing to start first and no port to keep free.
- **`payment_run`'s statement readers held to mock-bank's own writers**
  ([#6]), with moov-io's BAI2 and NACHA sample files.

### Changed

These differ from the last copies the mocks carried.

- **Money arriving is not posted to SAP as a payment coming back** ([#3]).
  `payment_run` tells a returned payment from a received credit, and posts a
  received credit without the reference it quotes, so that SAP does not reopen
  a paid invoice. It records a problem on the run each time. This is a stopgap
  until a `FINSTA01` can say which kind of credit a line is
  (rseufert/mock-sap#89).
- **A statement is posted to SAP in its own currency** ([#3]). The `FINSTA01`
  carried EUR whatever the statement said. A statement that names no currency
  is not posted, and the run says so. A BAI2 statement with no currency on the
  account or the group is read as USD.

### Known to be wrong

- **A second payment run started before the bank's statement is posted pays
  the same invoice again** ([#2]). SAP has no state between open and cleared
  for a run to write (rseufert/mock-sap#90). A test states this behaviour so
  that it stays visible.

[0.3.2]: https://github.com/rseufert/mock-acme/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/rseufert/mock-acme/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/rseufert/mock-acme/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rseufert/mock-acme/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rseufert/mock-acme/releases/tag/v0.1.0
[#1]: https://github.com/rseufert/mock-acme/pull/1
[#2]: https://github.com/rseufert/mock-acme/issues/2
[#3]: https://github.com/rseufert/mock-acme/pull/3
[#4]: https://github.com/rseufert/mock-acme/pull/4
[#6]: https://github.com/rseufert/mock-acme/pull/6
[#8]: https://github.com/rseufert/mock-acme/issues/8
[#12]: https://github.com/rseufert/mock-acme/issues/12
[#13]: https://github.com/rseufert/mock-acme/issues/13
[#15]: https://github.com/rseufert/mock-acme/issues/15
[#18]: https://github.com/rseufert/mock-acme/issues/18
[#21]: https://github.com/rseufert/mock-acme/issues/21
[#25]: https://github.com/rseufert/mock-acme/issues/25
[#27]: https://github.com/rseufert/mock-acme/issues/27
[#30]: https://github.com/rseufert/mock-acme/issues/30
[#34]: https://github.com/rseufert/mock-acme/issues/34
