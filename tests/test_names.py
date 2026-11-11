import unittest

from debrief_uploader import names


class TestNames(unittest.TestCase):
    def test_map_codes_read_as_the_game_names_them(self):
        self.assertEqual(names.map_name("spaces/49_Shoreside"), "Scandinavia")
        self.assertEqual(names.map_name("spaces/52_Britain"), "Crash Zone")
        self.assertEqual(names.map_name("50_Gold_harbor"), "Haven")

    def test_an_unlisted_map_still_reads(self):
        self.assertEqual(names.map_name("spaces/62_New_Map"), "New Map")
        self.assertEqual(names.map_name(None), "")

    def test_ships_and_battle_types(self):
        self.assertEqual(names.ship_name("PJSC008-Myoko"), "Myoko")
        self.assertEqual(names.ship_name("PJSB013-Amagi-1942"), "Amagi 1942")
        self.assertEqual(names.battle_type("pvp"), "Standard")
        self.assertEqual(names.battle_type("ranked"), "Ranked")
        self.assertEqual(names.battle_type("war_tales_event"), "War tales event")
        self.assertEqual(names.battle_type(None), "")


if __name__ == "__main__":
    unittest.main()
