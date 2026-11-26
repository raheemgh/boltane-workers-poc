"""test_db - the database port is a runtime-checkable protocol that the fake database satisfies.

Level:   L1
Stage:   V2-02B
"""

import unittest

from app.v2.ports.db import DbPort
from v2_inbox_model import FakeDb


class PortTest(unittest.TestCase):
    def test_db_port__the_fake_database_satisfies_it(self):
        self.assertIsInstance(FakeDb(), DbPort)

    def test_db_port__an_object_without_rpc_does_not(self):
        self.assertNotIsInstance(object(), DbPort)


if __name__ == "__main__":
    unittest.main()
