"""What makes the runtime binding rather than advisory.

Network isolation and separate images stop an agent that plays by the rules. The thing
that stops one that does not is the actuator refusing to move without an authorization
receipt. The ledger id is that receipt: the runtime writes the entry before it acts, and
the entry id travels with the command.
"""

import unittest

from sim.world import Simulation


class ReceiptTest(unittest.TestCase):
    def test_an_open_actuator_obeys_anyone(self):
        world = Simulation(lock_actuator=False).worlds["direct"]
        result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, None, "schedule",
                           None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.unrecorded_actions, 1)

    def test_a_locked_actuator_refuses_a_command_with_no_receipt(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, None, "schedule",
                           None, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 1)
        self.assertEqual(world.vehicles["taxi-a"].assigned_pad, None)

    def test_a_locked_actuator_still_obeys_the_runtime(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, "l_abc123",
                           "schedule", None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 0)
        self.assertEqual(world.score.unrecorded_actions, 0)

    def test_locking_costs_nothing_when_everyone_already_goes_through(self):
        """런타임을 거치는 쪽은 잠그든 안 잠그든 결과가 같습니다."""
        for locked in (False, True):
            world = Simulation(lock_actuator=locked).worlds["guarded"]
            result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, "l_1",
                               "schedule", None, 1)
            self.assertTrue(result["ok"], f"locked={locked}")


if __name__ == "__main__":
    unittest.main()
