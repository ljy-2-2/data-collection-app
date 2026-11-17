"""Approved plan A: locked shoulder joints, downward TCP and CAD height."""
import json
from pathlib import Path
import sys
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'),str(ROOT/'teleop_control/vendor/tianji/teleop')]
from qt_ui.poses import builtin_poses
from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import rotation, transform
from marvin_teleop.model import Models

PREVIOUS = dict(left=[90,-90,-90,-105,135,0,0],
                right=[-90,-90,90,-105,-135,0,0])


class WorkPoseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = Models(configuration(
            ROOT/'teleop_control/vendor/tianji/teleop/marvin_teleop/config.json'))
        cls.targets = builtin_poses('tianji')[0]['targets']

    def test_plan_a_locks_first_three_joints_and_reaches_approved_tcp(self):
        cad = json.loads((ROOT/'public/tianji-model.json').read_text())
        display = np.linalg.inv(np.asarray(cad['world_from_display']))
        stand = next(link for link in cad['links'] if link['name']=='stand')
        floors = []
        for visual in stand['visuals']:
            vertices = np.asarray(cad['meshes'][visual['mesh']]['vertices'])*visual['scale']
            t = transform(visual['xyz'],visual['rpy'])
            floors.append((vertices @ t[:3,:3].T+t[:3,3])[:,2].min())
        for side, q in self.targets.items():
            with self.subTest(side=side):
                self.assertEqual(q[:3],PREVIOUS[side][:3])
                arm = self.models.arms[side]
                tcp = arm.fk(q)
                expected = rotation([0,1,0],np.pi/4) @ arm.fk(PREVIOUS[side])[:3,:3]
                np.testing.assert_allclose(tcp[:3,:3],expected,atol=1e-7)
                self.assertAlmostEqual(np.degrees(np.arcsin(tcp[2,0])),-30.,places=5)
                self.assertAlmostEqual((display@tcp)[2,3]-min(floors),.9853437334,places=6)
                faces,bounds = arm.joint_halfspaces(margin=0)
                self.assertGreater(np.min((bounds-faces@q)/np.linalg.norm(faces,axis=1)),3.)

    def test_sampled_old_to_new_transition_stays_inside_model_constraints(self):
        # Only sampled robot link/base checks, not an arbitrary live entry path.
        for fraction in np.linspace(0,1,241):
            pair = {s:np.asarray(PREVIOUS[s])*(1-fraction)+np.asarray(q)*fraction
                    for s,q in self.targets.items()}
            self.assertTrue(all(self.models.arms[s].valid_joints(q) for s,q in pair.items()))
            self.assertIsNone(self.models.collision_reason(pair,.015))


if __name__=='__main__':
    unittest.main()
