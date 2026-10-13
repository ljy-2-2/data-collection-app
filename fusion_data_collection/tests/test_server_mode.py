import sys
from pathlib import Path
import unittest
from unittest.mock import Mock
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'fusion_camera_stream/controller'))
from streaming import JointSampler

class ServerModeTests(unittest.TestCase):
    def sample(self,stationary,error_state=0):
        api=Mock();api.monotonic.return_value=10.
        reader=Mock();reader.sample.return_value={'frame_serial':[1,1],
            'velocity_deg_s':[20.]*14,'arm_state':[0,0,error_state,0,0,0]}
        sampler=JointSampler(api,reader,seconds=1,hz=1,stationary_only=stationary)
        sampler.run()
        return sampler
    def test_default_stationary_guard_preserved(self):
        s=self.sample(True)
        self.assertIn('Stationary',s.error)
        self.assertEqual(s.count,0)
    def test_explicit_observation_allows_reading_movement(self):
        s=self.sample(False)
        self.assertIsNone(s.error);self.assertEqual(s.count,1)
        self.assertEqual(s.pop()['velocity_deg_s'],[20.]*14)
    def test_robot_error_guard_remains_in_observation_mode(self):
        s=self.sample(False,1)
        self.assertIn('arm error',s.error);self.assertEqual(s.count,0)

if __name__=='__main__':unittest.main()
