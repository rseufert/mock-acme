# mock-acme

**The integration between the mocks.** [mock-sap](https://github.com/rseufert/mock-sap),
[mock-edi](https://github.com/rseufert/mock-edi),
[mock-bank](https://github.com/rseufert/mock-bank) and
[mock-einvoice](https://github.com/rseufert/mock-einvoice) stand in for systems you do
not own, so that the code between them can be tested. This is that code: ACME's
middleware, the fourth actor in every story the mocks tell.

It is not a mock, and it is not a client library to put in front of real money.
It is a reference: worked integrations, each with tests that run against the
released mocks, kept in one place so that there is one copy of each.

## What is here

| Module | Carries | Between |
| --- | --- | --- |
| `mockacme.po_bridge` | a purchase order out as an 850, the 855 back as an ORDRSP IDoc | SAP, supplier |
| `mockacme.invoice_check` | the supplier's 856 and 810, matched to the order, posted as an INVOIC IDoc or blocked, or held while the invoice's own ship notice has not arrived | SAP, supplier |
| `mockacme.pay_invoices` | a supplier's 810s, paid as a pain.001 and followed to the statement | supplier, bank |
| `mockacme.payment_run` | SAP's open items, paid as a pain.001 or a NACHA file, cleared by posting the statement as a FINSTA01 | SAP, bank |
| `mockacme.remittance` | SAP's payment advice (a PEXR2002 it generates) out as an 820, and what the supplier made of it | SAP, supplier |
| `mockacme.procure_to_pay` | one purchase from the order to the cleared payment, and the supplier told what it was for; a backorder as a second payable on the same order; an invoice ahead of its ship notice, not paid until the notice comes | all three |
| `mockacme.e_invoice` | the supplier's invoice as a UBL e-invoice, matched and posted as an 810 is, and answered with Peppol Invoice Responses: received, accepted or rejected with the reason, and paid when SAP has cleared it | SAP, supplier (e-invoicing and EDI) |

`mockacme.bank_messages` is what the two payment modules share: the call to the
bank and the reading of its ISO 20022 answers.

Every module talks to the mocks over HTTP and imports none of them. The package
has no dependencies.

## Installing

```bash
python3 -m pip install mock-acme
```

That installs the package and nothing else. The mocks it talks to are separate:
`pip install mock-sap mock-edi mock-bank mock-einvoice`.

`payment_run` needs **mock-sap 0.19.0 or later**. It writes which payment run
has an invoice on the invoice itself, which an older mock-sap refuses, so
nothing is paid. And against an older one, a customer's payment that quotes
the number of an invoice already paid reopens that invoice.

## Tested against

What each release was tested against: the mocks CI installed for the run on
the release's own commit.

| mock-acme | mock-sap | mock-edi | mock-bank | mock-einvoice |
| --- | --- | --- | --- | --- |
| 0.5.0 | 0.21.0 | 0.9.0 | 0.9.0 | 0.1.0 |
| 0.4.0 | 0.21.0 | 0.9.0 | 0.9.0 | 0.1.0 |
| 0.3.2 | 0.19.0 | 0.8.0 | 0.9.0 | not used |
| 0.3.1 | 0.19.0 | 0.8.0 | 0.9.0 | not used |
| 0.3.0 | 0.19.0 | 0.7.0 | 0.7.0 | not used |
| 0.2.0 | 0.18.0 | 0.7.0 | 0.7.0 | not used |
| 0.1.0 | 0.17.1 | 0.7.0 | 0.7.0 | not used |

A row is what was run, and nothing else. It does not say an earlier or a later
mock fails. The lowest versions the tests are known to need are the floors in
the `test` extra in [`pyproject.toml`](pyproject.toml), and nothing runs the
tests at those floors, so they say "not below this" and no more.

Rows are not typed in. [`tools/tested_against.py`](tools/tested_against.py)
writes one from the mocks installed where the tests have just run, and
`publish.yml` refuses to publish a release whose row is not what its own run
installed. The rows up to 0.5.0 are older than the tool, and were read from
the CI logs of each release commit.

## Running the tests

From a checkout:

```bash
python3 -m pip install -e ".[test]"
python3 -m unittest discover -s tests -t . -v
```

That is the whole arrangement. Importing `tests` starts the four mocks, each on
a port the operating system chose, and stops them afterwards.
[`tests/__init__.py`](tests/__init__.py) says how to point the tests at a mock
that is already running, which is how they are run against a mock's `main`.

`tests/test_payment_run_readers.py` is the one test that imports a mock rather
than talking to it: it gives `payment_run`'s hand-written statement readers the
same statement mock-bank wrote as a `camt.053` and as BAI2, and five BAI2 files
from [moov-io/bai2](https://github.com/moov-io/bai2), and holds them to
mock-bank's own reader. The samples and their licence are in
[`tests/samples/external/`](tests/samples/external/SOURCES.md).

`payment_run` pays by ACH as well. The same tests in that mode:

```bash
PAYMENT_RUN_FORMAT=nacha python3 -m unittest -v tests.test_payment_run
```

The released mocks are what a pull request is tested against. A second
workflow, [`mocks-main.yml`](.github/workflows/mocks-main.yml), runs the same
tests every night against each mock's `main`, so that a change merged there
which breaks this code is seen before it is released. It is not a required
check; a red run there says something about another repository. To do the same
by hand:

```bash
python3 -m pip install \
  "mock-sap @ git+https://github.com/rseufert/mock-sap@main" \
  "mock-edi @ git+https://github.com/rseufert/mock-edi@main" \
  "mock-bank @ git+https://github.com/rseufert/mock-bank@main" \
  "mock-einvoice @ git+https://github.com/rseufert/mock-einvoice@main"
```

## Known to be wrong

Two payment runs that select at the same moment can both pay an invoice. A run
says in SAP which run has each item before its file goes out, as `PaymentRunID`
and `PaymentRunDate` on the invoice, and every other run leaves that item alone
until the bank refuses the payment, SAP clears it, or the payment comes back
([#2](https://github.com/rseufert/mock-acme/issues/2),
[#21](https://github.com/rseufert/mock-acme/issues/21)). But reading the open
items and writing the claim are two requests, and nothing makes them one: two
runs that both read before either writes both pay. One run after another, on
any machine, is safe; two at once are not.

An e-invoice posted by a process that stopped before saying so is never
answered. `e_invoice` keeps what it has collected and said in memory, and a
new process asks the supplier's side what each invoice was last told, so
nothing is acknowledged, accepted or rejected twice, and an invoice accepted
before a restart is still told "paid". But when the last thing said was
"received" and SAP already holds the invoice, there are two ways that came
about: the earlier process posted it and stopped before it said "accepted", or
the invoice is a copy of one that reached SAP another way. SAP holds a number
and a supplier, not who posted it. So the invoice is reported as blocked with
both reasons, the supplier is told nothing, and "paid" is never said for it. A
person has to look. An XRechnung invoice, which is told nothing, is reported
as blocked for being in SAP after every restart, for the same reason.

What a shipment has been billed is one process's memory. `invoice_check`
counts each invoice against the shipment it names, so a supplier that bills
one delivery twice under two invoice numbers has the second blocked
([#41](https://github.com/rseufert/mock-acme/issues/41)). A restart loses the
count, in `DurableInvoiceCheck` as well: it asks SAP what an order has been
billed, and SAP's supplier invoice has nowhere to say which shipment it was
for, in mock-sap and so here. A new process holds such an invoice for a ship
notice it never saw; if the supplier sends that notice again, the invoice is
posted. And an invoice that names no shipment is not counted against one at
all: two of them for parts of one delivery are each compared with the order's
latest notice on its own. mock-edi sends neither a second invoice for a
shipment nor one that names none, so the tests write them.

A held invoice waits for ever if its ship notice never comes, and is reported
as held on every run. What is held, like what has shipped, is one process's
memory: a restart loses it, and the invoice with it, since collecting from the
mailbox took it out.

It has other edges:

- An e-invoice names no shipment, so `e_invoice` never holds one: an
  e-invoice for more than the order's latest ship notice is rejected.
- `e_invoice` does not read allowances or charges, so an e-invoice with one is
  rejected because it does not add up. It does not read a credit note at all.
  A part payment is not told as one: a payable is cleared or it is not.
- Which of Peppol's reason codes an e-invoice is rejected with is this
  package's reading: its list has no code for a duplicate, which is told as a
  wrong reference (`REF`), nor for a currency, which is told as other (`OTH`).

- A run's identification is one to six characters, which is SAP's own limit.
  A longer one is refused before anything is selected.
- When the bank refuses a payment and SAP cannot then be reached, the claim
  stays on the item and no run selects it. The run says so as a problem, and a
  person has to take the claim off.
- `Register`, the run's own record from before SAP could hold this, is still
  accepted and no longer needed. Passing one means two records of one fact:
  whoever posts the statements has to be given the same register, or its entry
  outlives the claim and holds an item SAP says is free.

A payment the bank reports without its reference is not cleared. A statement
line that quotes nothing goes to SAP quoting nothing, mock-sap matches no item
to it (it does not fall back to the amount), and the invoice stays open and
claimed by the run that paid it, so no later run pays it either
([#44](https://github.com/rseufert/mock-acme/issues/44)). The run says so as a
problem, with SAP's reason and its own accepted payments of that amount
([#46](https://github.com/rseufert/mock-acme/issues/46)), and a person has to
clear the item or take the claim off. It does the same for any debit SAP
places nowhere, which includes one that was never this run's: the run cannot
tell. mock-bank always gives the reference back, so the first case takes a
statement from somewhere else.

Money arriving is posted to SAP and nothing comes of it. Each credit on the
statement says whether it is a payment coming back or money arriving, so SAP
no longer takes a receipt for a return, but posting it against a receivable is
not built ([mock-sap#65](https://github.com/rseufert/mock-sap/issues/65)). SAP
answers the line as unprocessed, and that is on the statement's record.

## Releasing

A release is the same three acts as in the mocks,
by one person in one sitting: merge a pull request that sets `version` in
`pyproject.toml` and `__version__` in `mockacme/__init__.py` and dates the
section in [`CHANGELOG.md`](CHANGELOG.md), and has the release's row under
[Tested against](#tested-against), written by `python3 tools/tested_against.py
--write` after the tests have passed; tag that commit `v<version>`; publish
a GitHub Release from the tag. Publishing the Release runs
[`publish.yml`](.github/workflows/publish.yml), which runs the tests, builds,
refuses a tag that disagrees with the package or a "Tested against" row that
disagrees with the mocks it just tested with, and uploads to PyPI through
Trusted Publishing. Running that workflow by hand uploads to TestPyPI instead.

There is no `tools/release.py` here as there is in the mocks: the steps are done
by hand. CI checks that the two places the version is written agree, and that
the version has a row under "Tested against".

## Where the code came from

The modules were copied on 2026-10-04 from the `examples/` folder of the mock
each was written beside, with their tests; `remittance` was written there
afterwards and followed on 2026-10-05. Nothing in them changed but imports,
the lines saying how to run the tests, and issue references, which now name the
repository they belong to.

| Here | From | At |
| --- | --- | --- |
| `po_bridge`, its tests | mock-edi `examples/` | `0e86279` (0.7.0) |
| `invoice_check`, its tests | mock-sap `examples/` | `0ea524d` |
| `remittance`, its tests | mock-sap `examples/` | `73055a2` |
| `bank_messages`, `pay_invoices`, `payment_run`, `procure_to_pay`, their tests | mock-bank `examples/` | `fd618e6` (0.7.0) |

The originals were removed from those repositories on 2026-10-05, and each
left an `examples/README.md` saying which file became which. This is the only
copy.

The repository's settings are described in [docs/GITHUB.md](docs/GITHUB.md).
