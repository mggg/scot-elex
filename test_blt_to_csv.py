import tempfile
import unittest
from pathlib import Path

from blt_to_csv import (
    _csv_style_name,
    process_2007_cands,
    process_2012_2017_cands,
    process_2022_cands,
    process_file_list,
)


class BltToCsvTest(unittest.TestCase):
    def test_preserves_names_parties_and_updates_both_views(self):
        self.assertEqual(_csv_style_name("Eòghann MACCOLL"), "Eòghann MacColl")
        self.assertEqual(
            _csv_style_name("Peter Ã“ DONNGHAILE"), "Peter Ó Donnghaile"
        )
        self.assertEqual(
            process_2007_cands(["# ALTERNATIVE NAME 1: Rory O'Brien (Con)"]),
            [("Rory O'Brien", "Conservative and Unionist Party (Con)")],
        )
        self.assertEqual(
            process_2022_cands(
                ['"Victoria PALMER‐DYER" "Scottish Green Party"']
            ),
            [("Victoria PALMER‐DYER", "Green (Gr)")],
        )
        self.assertEqual(
            process_2022_cands(
                ['"Katie PRAGNELL" "Labour and Co‐operative Party"']
            ),
            [("Katie PRAGNELL", "Labour and Co-operative Party (LabCo)")],
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "blt_files" / "2_cands" / "example_2012_ward1.blt"
            source.parent.mkdir(parents=True)
            source.write_text(
                "2 1\n"
                "1 1 0\n"
                "0\n"
                "Jane O'NEILL (Lab)\n"
                "Finlay ARCHIBALD (Pir)\n"
                '"Example, Ward"\n',
                encoding="utf-8",
            )

            output = root / "output"
            process_file_list([str(source)], output, process_2012_2017_cands)

            candidate_csv = output / "2_cands" / "example_2012_ward1.csv"
            seat_csv = output / "1_seats" / "example_2012_ward1.csv"
            self.assertEqual(candidate_csv.read_bytes(), seat_csv.read_bytes())
            csv_text = candidate_csv.read_text()
            self.assertIn("Jane O'Neill", csv_text)
            self.assertIn("Pirate (Pir)", csv_text)
            self.assertTrue(csv_text.endswith('"Example, Ward",'))


if __name__ == "__main__":
    unittest.main()
