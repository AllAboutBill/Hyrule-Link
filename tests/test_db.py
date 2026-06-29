import re
import unittest

from server import db


class RoomCodeTests(unittest.TestCase):
    def setUp(self):
        self.old_path, self.old_conn = db.DB_PATH, db._conn
        db.DB_PATH, db._conn = ":memory:", None
        db.init()

    def tearDown(self):
        db._conn.close()
        db.DB_PATH, db._conn = self.old_path, self.old_conn

    def test_room_codes_are_human_readable_and_high_entropy(self):
        code = db.create_room("Test")
        self.assertEqual(len(code), db.ROOM_CODE_LENGTH)
        self.assertRegex(code, re.compile(f"^[{db.ROOM_CODE_ALPHABET}]+$"))
        self.assertIsNotNone(db.get_room(code))


if __name__ == "__main__":
    unittest.main()
