import json
import threading
import unittest

from arr_subtitle.models import Anime, Budget, Cancelled
from arr_subtitle.serialization import dumps
from arr_subtitle.settings import parse_settings


class ModelTests(unittest.TestCase):
    def test_json_camelcase_and_unicode(self):
        text = dumps(Anime(1, "작품", "原題"))
        self.assertIn("작품", text)
        self.assertEqual(json.loads(text)["animeId"], 1)

    def test_budget_preserves_parent_cancel(self):
        event = threading.Event()
        child = Budget(30, event).child(10)
        event.set()
        with self.assertRaises(Cancelled):
            child.check()

    def test_settings_mapping_validation(self):
        settings = parse_settings({"mappings": [{"title": "작품", "season": 2, "animeId": 12, "aliases": ["Series"], "episodeOffset": 12}]})
        self.assertEqual(settings.mappings[0].episode_offset, 12)
        for data in (None, {"aniListEnabled": 1}, {"mappings": [{"title": "A", "season": True, "animeId": 1}]}):
            with self.assertRaises(ValueError):
                parse_settings(data)
