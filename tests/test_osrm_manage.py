import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("osrm_manage_under_test", ROOT / "osrm/manage.py")
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class MaxTableSizeTests(unittest.TestCase):
    def parse(self, *argv):
        return manage.build_parser().parse_args(["start", *argv])

    def test_default_is_2500_and_reaches_the_server_command(self):
        args = self.parse()
        self.assertEqual(args.max_table_size, 2500)
        command = manage.server_command(args)
        self.assertEqual(command[command.index("--max-table-size") + 1], "2500")

    def test_given_value_is_used(self):
        command = manage.server_command(self.parse("--max-table-size", "4000", "--port", "5001"))
        self.assertEqual(command[command.index("--max-table-size") + 1], "4000")
        self.assertEqual(command[command.index("--port") + 1], "5001")
        self.assertEqual(command[-1], str(manage.BASE))

    def test_zero_is_rejected(self):
        with self.assertRaises(SystemExit):
            manage.validate(manage.build_parser(), self.parse("--max-table-size", "0"))


if __name__ == "__main__":
    unittest.main()
