# External samples: where each one came from

These files were written outside this project. `tests/test_payment_run_readers.py`
reads them with `mockacme.payment_run`'s hand-written readers and with
mock-bank's own, and the two must agree.

They were copied from mock-bank's `tests/samples/external/` at `ded9818`, which
fetched each one, unmodified, from the commit named below. mock-bank's
[`SOURCES.md`](https://github.com/rseufert/mock-bank/blob/main/tests/samples/external/SOURCES.md)
says what each file settled there; this one says only what is here and under
what terms.

| File | Source | Path at that commit | SHA-256 |
| --- | --- | --- | --- |
| `bai2-sample1.txt` | [moov-io/bai2](https://github.com/moov-io/bai2) @ `d3e11b628d3d59fd6911836b9ca328cb8b7621f2` | `test/testdata/sample1.txt` | `0150331e6118e9fc6a1a10871f739b2d317c5cca5159c007622cffbbb64fe00c` |
| `bai2-sample2.txt` | same | `test/testdata/sample2.txt` | `34ccf04a37e44353e5aac16981201239ae90102c12806aaa739a2e13ae3aee6b` |
| `bai2-sample3.txt` | same | `test/testdata/sample3.txt` | `8a13ec611352000fbab9a880858e8349b50380fab9754bc237391738cfd9ada4` |
| `bai2-sample4.txt` | same | `test/testdata/sample4-continuations-newline-delimited.txt` | `5a11cde54c9c8266b34d9980ee66237c1311f56b87d9eb1d28e5f02bafebaa9f` |
| `bai2-sample5.txt` | same | `test/testdata/sample5-issue113.txt` | `0391a0999e718ee84048f1b9642be5b08f963c41f3cd628f3f8ac677fb9a2e5c` |
| `nacha-return-WEB.ach` | [moov-io/ach](https://github.com/moov-io/ach) @ `7ee7ad03d7342e1f651c32db22fc8168c2b97cce` | `test/testdata/return-WEB.ach` | not recorded by mock-bank |

The BAI2 digests are the ones mock-bank recorded when it fetched the files, and
the copies here match them. The files were not fetched again from moov-io for
this copy.

Both projects are **Apache License 2.0**, a copy of which is
`LICENSE-Apache-2.0.txt`. moov-io/ach has a `NOTICE` file, kept here as
`NOTICE-moov-ach.txt`; moov-io/bai2 has none. Copyright The Moov Authors.

`.gitattributes` declares this directory `-text` so that a Windows checkout does
not rewrite the line endings.
