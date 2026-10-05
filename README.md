# mock-acme

**The integration between the mocks.** [mock-sap](https://github.com/rseufert/mock-sap),
[mock-edi](https://github.com/rseufert/mock-edi) and
[mock-bank](https://github.com/rseufert/mock-bank) stand in for systems you do
not own, so that the code between them can be tested. This is that code: ACME's
middleware, the fourth actor in every story the mocks tell.

It is not a mock, and it is not a client library to put in front of real money.
It is a reference: worked integrations, each with tests that run against the
released mocks, kept in one place so that there is one copy of each.

## What is here

| Module | Carries | Between |
| --- | --- | --- |
| `mockacme.po_bridge` | a purchase order out as an 850, the 855 back as an ORDRSP IDoc | SAP, supplier |
| `mockacme.invoice_check` | the supplier's 856 and 810, matched to the order, posted as an INVOIC IDoc or blocked | SAP, supplier |
| `mockacme.pay_invoices` | a supplier's 810s, paid as a pain.001 and followed to the statement | supplier, bank |
| `mockacme.payment_run` | SAP's open items, paid as a pain.001 or a NACHA file, cleared by posting the statement as a FINSTA01 | SAP, bank |
| `mockacme.remittance` | SAP's payment advice (a PEXR2002 it generates) out as an 820, and what the supplier made of it | SAP, supplier |
| `mockacme.procure_to_pay` | one purchase from the order to the cleared payment | all three |

`mockacme.bank_messages` is what the two payment modules share: the call to the
bank and the reading of its ISO 20022 answers.

Every module talks to the mocks over HTTP and imports none of them. The package
has no dependencies.

## Running the tests

```bash
python3 -m pip install -e ".[test]"
python3 -m unittest discover -s tests -t . -v
```

That is the whole arrangement. Importing `tests` starts the three mocks, each on
a port the operating system chose, and stops them afterwards.
[`tests/__init__.py`](tests/__init__.py) says how to point the tests at a mock
that is already running, which is how they are run against a mock's `main`.

`payment_run` pays by ACH as well. The same tests in that mode:

```bash
PAYMENT_RUN_FORMAT=nacha python3 -m unittest -v tests.test_payment_run
```

## Known to be wrong

`payment_run` can pay an invoice twice: a second run started before the bank's
statement has been posted selects the same invoice again, and the bank pays it
again. SAP offers no state between open and cleared for a run to write
([mock-sap#90](https://github.com/rseufert/mock-sap/issues/90)), so this is not
fixed, and `tests/test_payment_run.py` has a test that says so. It is tracked
in [#2](https://github.com/rseufert/mock-acme/issues/2).

Money arriving is posted to SAP without the reference it quotes, so that SAP
does not take it for a returned payment and reopen an invoice. That is a
stopgap, said as a problem on the run each time it happens, until the FINSTA01
has a way to tell the two apart
([mock-sap#89](https://github.com/rseufert/mock-sap/issues/89)).

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

The originals are still in those repositories. Until they are removed there,
this is a third copy and not yet the only one.

The repository's settings are described in [docs/GITHUB.md](docs/GITHUB.md).
