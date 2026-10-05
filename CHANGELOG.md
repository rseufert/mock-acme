# Changelog

Every release of [mock-acme](https://pypi.org/project/mock-acme/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

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

### Changed

- **A bank that does not answer leaves the run's items held under that run**
  ([#2]). The items stay `selected`, as before, and the same run can be sent
  again. A different run no longer pays them, because a request that timed
  out may have arrived.

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

[0.1.0]: https://github.com/rseufert/mock-acme/releases/tag/v0.1.0
[#1]: https://github.com/rseufert/mock-acme/pull/1
[#2]: https://github.com/rseufert/mock-acme/issues/2
[#3]: https://github.com/rseufert/mock-acme/pull/3
[#4]: https://github.com/rseufert/mock-acme/pull/4
[#6]: https://github.com/rseufert/mock-acme/pull/6
[#8]: https://github.com/rseufert/mock-acme/issues/8
