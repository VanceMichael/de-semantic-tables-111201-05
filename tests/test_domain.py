import json
from pathlib import Path
import unittest

from collector.domain import *


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(Path('fixtures/sample.json').read_text())

    def test_domain_behavior(self):
        self.assertEqual(amount('1,200.50'), '1200.50')
        self.assertEqual(collect(self.payload)[0]['quantity'], 2)
        with self.assertRaises(ValueError):
            amount('NaN')

    def test_deterministic_observations(self):
        self.assertEqual(collect(self.payload), collect(self.payload))
