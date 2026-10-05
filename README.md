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
| `mockacme.procure_to_pay` | one purchase from the order to the cleared payment, and the supplier told what it was for | all three |

`mockacme.bank_messages` is what the two payment modules share: the call to the
bank and the reading of its ISO 20022 answers.

Every module talks to the mocks over HTTP and imports none of them. The package
has no dependencies.

## Installing

```bash
python3 -m pip install mock-acme
```

That installs the package and nothing else. The mocks it talks to are separate:
`pip install mock-sap mock-edi mock-bank`.

## Running the tests

From a checkout:

```bash
python3 -m pip install -e ".[test]"
python3 -m unittest discover -s tests -t . -v
```

That is the whole arrangement. Importing `tests` starts the three mocks, each on
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
  "mock-bank @ git+https://github.com/rseufert/mock-bank@main"
```

## Known to be wrong

SAP has no state between open and cleared
([mock-sap#90](https://github.com/rseufert/mock-sap/issues/90)), so an invoice a
payment run has sent to the bank still looks open to the next run. `payment_run`
keeps its own record instead: a `Register` of what it has sent, written before
the file goes out, which a later run reads and leaves those items alone until
the bank refuses the payment or SAP clears it
([#2](https://github.com/rseufert/mock-acme/issues/2)).

That is one company's own record and not SAP's, and it has edges:

- `PaymentRun(...)` with no `register` keeps it in memory, which covers that
  object's runs and nothing else. A payment program that is started again each
  day has to pass `register=Register(path)`.
- Two installations with two registers pay twice, and so do two processes
  writing one file at the same moment. Nothing here locks it.
- Whoever posts the statements needs the same register, or its entries are
  never let go.

Money arriving is posted to SAP without the reference it quotes, so that SAP
does not take it for a returned payment and reopen an invoice. That is a
stopgap, said as a problem on the run each time it happens, until the FINSTA01
has a way to tell the two apart
([mock-sap#89](https://github.com/rseufert/mock-sap/issues/89)).

## Releasing

A release is the same three acts as in the mocks,
by one person in one sitting: merge a pull request that sets `version` in
`pyproject.toml` and `__version__` in `mockacme/__init__.py` and dates the
section in [`CHANGELOG.md`](CHANGELOG.md); tag that commit `v<version>`; publish
a GitHub Release from the tag. Publishing the Release runs
[`publish.yml`](.github/workflows/publish.yml), which runs the tests, builds,
refuses a tag that disagrees with the package, and uploads to PyPI through
Trusted Publishing. Running that workflow by hand uploads to TestPyPI instead.

There is no `tools/release.py` here as there is in the mocks: the steps are done
by hand, and CI checks only that the two places the version is written agree.

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
