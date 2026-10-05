"""mock-acme: the integration between the mocks.

ACME is the company in every story the mocks tell: the buyer that sends the
purchase order, the SAP system that owes the invoice, the account the bank
pays from. This package is ACME's middleware, the code that carries a document
from one system to the next:

    po_bridge       SAP purchase order -> 850 -> supplier; 855 -> SAP
    invoice_check   856 and 810 from the supplier, matched to the order, posted to SAP
    pay_invoices    810s from the supplier, paid through the bank
    payment_run     SAP's open items, paid through the bank and cleared by its statement
    procure_to_pay  all of it, across all three mocks

It is not a mock. mock-sap, mock-edi and mock-bank stand in for systems you do
not own so that code like this can be tested; this is that code. It talks to
them over HTTP and imports none of them.

It is a reference, not a client library to put in front of real money:
`payment_run` can still pay an invoice twice in the ways rseufert/mock-bank#164
lists. Standard library only.
"""
__version__ = "0.1.0.dev0"
