"""Contracts for pure, bounded terminal artwork and its fallback projection."""

from dataclasses import FrozenInstanceError
import unittest

import porkyhud_visual as visual


class MascotTests(unittest.TestCase):
    def test_every_pose_is_deterministic_fixed_size_and_semantic(self) -> None:
        frames = set()
        roles = set()
        for phase in range(visual.MASCOT_CYCLE):
            frame = visual.render_mascot(phase)
            self.assertEqual(frame, visual.render_mascot(phase + visual.MASCOT_CYCLE))
            self.assertEqual((frame.width, frame.height), (28, 7))
            self.assertEqual(len(frame.rows), frame.height)
            self.assertTrue(all(len(row) == frame.width for row in frame.plain_rows))
            frames.add(frame.plain_rows)
            for row in frame.rows:
                self.assertTrue(all(span.text for span in row))
                self.assertTrue(all(left.role != right.role
                                    for left, right in zip(row, row[1:])))
                for span in row:
                    roles.add(span.role)
                    self.assertTrue(all(character == " "
                                        or 0x2800 <= ord(character) <= 0x28ff
                                        for character in span.text))
        self.assertGreater(len(frames), 24)
        self.assertEqual(roles, set(visual.VISUAL_ROLES))

    def test_rest_pose_is_stable_and_blink_does_not_destroy_silhouette(self) -> None:
        resting = visual.render_mascot(0)
        self.assertEqual(resting, visual.render_mascot(96 * 1_000_000))
        self.assertNotEqual(visual.render_mascot(56), visual.render_mascot(57))
        # The outer two cells remain breathing room in every pose, preventing
        # motion from touching adjacent title text or the owning rectangle.
        for phase in range(visual.MASCOT_CYCLE):
            for row in visual.render_mascot(phase).plain_rows:
                self.assertEqual(row[:2], "  ")
                self.assertEqual(row[-2:], "  ")
        with self.assertRaises(FrozenInstanceError):
            resting.width = 10

    def test_ascii_and_outline_are_explicit_complete_variations(self) -> None:
        for phase in (0, 24, 48, 57, 72, 95, -1):
            forced = visual.render_mascot(phase, "ascii")
            fallback = visual.render_mascot(phase, unicode_enabled=False)
            self.assertEqual(forced, fallback)
            self.assertEqual(len(forced.rows), 7)
            self.assertTrue(all(len(row) == 28 for row in forced.plain_rows))
            self.assertTrue(all(ord(character) < 128
                                for row in forced.plain_rows for character in row))
            self.assertIn("(oo)", "".join(forced.plain_rows))
        self.assertNotEqual(visual.render_mascot(0, "outline"),
                            visual.render_mascot(0, "sculpted"))

    def test_invalid_inputs_do_not_reach_the_cache(self) -> None:
        for phase in (True, 1.0, None, "1"):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                visual.render_mascot(phase)
        with self.assertRaises(ValueError):
            visual.render_mascot(0, "giant")
        with self.assertRaises(ValueError):
            visual.render_mascot(0, unicode_enabled="yes")

    def test_arbitrary_phase_values_leave_only_a_bounded_cache(self) -> None:
        for phase in range(visual.MASCOT_CYCLE * 3):
            visual.render_mascot(phase)
        self.assertLessEqual(visual._sculpture.cache_info().currsize,
                             visual.MASCOT_CYCLE)
        self.assertLessEqual(visual._render_frame.cache_info().currsize,
                             visual.MASCOT_CYCLE * 2)


class WordmarkTests(unittest.TestCase):
    def test_wordmark_variations_preserve_name_without_effects(self) -> None:
        self.assertEqual(visual.wordmark_lines(), ("P O R K Y H U D",))
        for unicode_enabled in (True, False):
            lines = visual.wordmark_lines("outline", unicode_enabled)
            self.assertEqual(len(lines), 2)
            self.assertIn("P O R K Y H U D", lines[0])
            if not unicode_enabled:
                self.assertTrue(all(ord(character) < 128
                                    for line in lines for character in line))
        with self.assertRaises(ValueError):
            visual.wordmark_lines("giant")


if __name__ == "__main__":
    unittest.main()
