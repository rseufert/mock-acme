# mock-acme

The integration between the mocks: the middleware that mock-sap, mock-edi and mock-bank exist to test. Zero dependencies.

**Nothing is built yet.** This repository will hold the worked integrations that today live in the three mocks' `examples/` folders (`po_bridge`, `invoice_check`, `pay_invoices`, `payment_run`, `procure_to_pay`), one copy of each, and the tests that run them against [mock-sap](https://github.com/rseufert/mock-sap), [mock-edi](https://github.com/rseufert/mock-edi) and [mock-bank](https://github.com/rseufert/mock-bank) together.

It is not a mock. It is the thing the mocks are there to test: a reference middleware, not something to run in production.

The repository's settings are described in [docs/GITHUB.md](docs/GITHUB.md).
