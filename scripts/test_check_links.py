import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_links


class RepoLinksTest(unittest.TestCase):
    def test_repository_has_no_broken_links(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(check_links.broken_links(root), [])


if __name__ == '__main__':
    unittest.main()
