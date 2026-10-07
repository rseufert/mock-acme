"""The README's "Tested against" table, and the tool that keeps it (#43).

    python3 -m unittest -v tests.test_tested_against

No mock is asked anything here. What is held is that the version this checkout
says it is has a row, and that the tool reads and writes the table the way the
release relies on.
"""
import importlib.util
import os
import unittest
import unittest.mock

import mockacme

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "tested_against", os.path.join(ROOT, "tools", "tested_against.py"))
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

README = """# A package

## Tested against

| mock-acme | mock-sap | mock-edi | mock-bank | mock-einvoice |
| --- | --- | --- | --- | --- |
| 0.4.0 | 0.21.0 | 0.9.0 | 0.9.0 | 0.1.0 |
| 0.3.0 | 0.19.0 | 0.7.0 | 0.7.0 | not used |

Words after it.

| another | table |
| --- | --- |
| 0.4.0 | left alone |
| 0.2.0 | left alone |
"""
NOW = {"mock-sap": "0.22.0", "mock-edi": "0.9.0", "mock-bank": "0.10.0",
       "mock-einvoice": "0.2.0"}


class TheTableInThisCheckout(unittest.TestCase):

    def rows(self):
        with open(tool.README, encoding="utf-8") as handle:
            return tool.table(handle.read())

    def test_the_version_here_has_a_row(self):
        """So a release cannot be made without one: the pull request that sets
        the version fails here until `--write` has been run."""
        self.assertEqual(tool.own_version(), mockacme.__version__)
        self.assertIn(mockacme.__version__, [version for version, _ in self.rows()])

    def test_the_rows_are_one_for_each_release_newest_first(self):
        versions = [tool.number(version) for version, _ in self.rows()]
        self.assertEqual(versions, sorted(set(versions), reverse=True))

    def test_every_row_names_a_version_of_each_mock_or_says_it_was_not_used(self):
        for version, mocks in self.rows():
            for mock, tested in mocks.items():
                self.assertRegex(tested, r"^(\d+\.\d+\.\d+)?$", (version, mock))

    def test_the_test_extra_names_only_mocks_the_table_has_a_column_for(self):
        self.assertEqual(sorted(tool.floors()), sorted(tool.MOCKS))


class ReadingAndWritingARow(unittest.TestCase):

    def test_a_version_is_compared_as_numbers(self):
        self.assertGreater(tool.number("0.21.0"), tool.number("0.9.0"))

    def test_the_table_is_read_up_to_the_first_line_that_is_not_a_row(self):
        self.assertEqual(tool.table(README), [
            ("0.4.0", {"mock-sap": "0.21.0", "mock-edi": "0.9.0", "mock-bank": "0.9.0",
                       "mock-einvoice": "0.1.0"}),
            ("0.3.0", {"mock-sap": "0.19.0", "mock-edi": "0.7.0", "mock-bank": "0.7.0",
                       "mock-einvoice": ""})])

    def test_a_readme_without_the_table_is_refused(self):
        for write in (tool.table, lambda text: tool.written(text, "0.5.0", NOW)):
            with self.assertRaises(ValueError):
                write("# A package\n\nNo table.\n")

    def test_a_row_with_a_cell_missing_is_refused(self):
        with self.assertRaises(ValueError):
            tool.table(README.replace("| 0.9.0 | 0.9.0 | 0.1.0 |", "| 0.9.0 | 0.1.0 |"))

    def test_a_new_release_is_put_first_and_nothing_else_is_touched(self):
        after = tool.written(README, "0.5.0", NOW)
        self.assertEqual([version for version, _ in tool.table(after)],
                         ["0.5.0", "0.4.0", "0.3.0"])
        self.assertEqual(tool.table(after)[0][1], NOW)
        self.assertEqual(after.replace("| 0.5.0 | 0.22.0 | 0.9.0 | 0.10.0 | 0.2.0 |\n", ""),
                         README)

    def test_a_release_that_has_a_row_has_it_replaced_where_it_is(self):
        after = tool.written(README, "0.3.0", NOW)
        self.assertEqual([version for version, _ in tool.table(after)], ["0.4.0", "0.3.0"])
        self.assertEqual(tool.table(after)[1][1], NOW)
        # The other table's 0.4.0 row, further down, is not this table's.
        self.assertIn("| 0.4.0 | left alone |", tool.written(README, "0.4.0", NOW))
        self.assertEqual(tool.written(README, "0.4.0", NOW).count("0.22.0"), 1)

    def test_a_row_of_some_later_table_is_not_taken_for_this_release(self):
        after = tool.written(README, "0.2.0", NOW)
        self.assertEqual([version for version, _ in tool.table(after)],
                         ["0.2.0", "0.4.0", "0.3.0"])
        self.assertIn("| 0.2.0 | left alone |", after)

    def test_a_mock_that_is_not_installed_here_is_found_as_none(self):
        def only_sap(name):
            if name != "mock-sap":
                raise tool.metadata.PackageNotFoundError(name)
            return "0.21.0"
        with unittest.mock.patch.object(tool.metadata, "version", only_sap):
            self.assertEqual(tool.installed(), {
                "mock-sap": "0.21.0", "mock-edi": "", "mock-bank": "",
                "mock-einvoice": ""})

    def test_a_mock_that_is_not_installed_is_written_as_not_used(self):
        after = tool.written(README, "0.5.0", dict(NOW, **{"mock-einvoice": ""}))
        self.assertIn("| 0.5.0 | 0.22.0 | 0.9.0 | 0.10.0 | not used |\n", after)

    def test_a_row_that_is_what_is_installed_has_nothing_against_it(self):
        self.assertEqual(tool.disagreements(tool.written(README, "0.5.0", NOW),
                                            "0.5.0", NOW), [])

    def test_a_release_with_no_row_is_told_how_to_get_one(self):
        [wrong] = tool.disagreements(README, "0.5.0", NOW)
        self.assertIn("no row for mock-acme 0.5.0", wrong)
        self.assertIn("--write", wrong)

    def test_each_mock_that_differs_is_named_with_both_versions(self):
        wrong = tool.disagreements(README, "0.4.0", dict(NOW, **{"mock-einvoice": ""}))
        self.assertEqual(wrong, [
            "README.md says mock-acme 0.4.0 was tested against mock-sap 0.21.0, and "
            "what is installed here is 0.22.0",
            "README.md says mock-acme 0.4.0 was tested against mock-bank 0.9.0, and "
            "what is installed here is 0.10.0",
            "README.md says mock-acme 0.4.0 was tested against mock-einvoice 0.1.0, "
            "and what is installed here is not installed"])
        [unused] = tool.disagreements(README, "0.3.0", dict(
            tool.table(README)[1][1], **{"mock-einvoice": "0.2.0"}))
        self.assertIn("mock-einvoice not used, and what is installed here is 0.2.0", unused)

    def test_the_mocks_installed_here_are_found(self):
        found = tool.installed()
        self.assertEqual(sorted(found), sorted(tool.MOCKS))
        # The tests are running against them, so each is there to be found.
        for mock in tool.MOCKS:
            self.assertRegex(found[mock], r"^\d+\.\d+", mock)


if __name__ == "__main__":
    unittest.main()
