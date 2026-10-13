"""Named rigid URDF tools: no SDK connection, enable, CAN or motion."""
import json,sys,tempfile,unittest
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
VENDOR=ROOT/'teleop_control/vendor/tianji'
sys.path.insert(0,str(VENDOR/'teleop'))
from marvin_teleop.tool_frame import resolve_urdf_tool,parse_tool_frame,validate_tool_config
from marvin_teleop.configuration import configuration
from marvin_teleop.geometry import transform

class URDFToolTests(unittest.TestCase):
    def write(self,path,joint_type='fixed',xyz='0.01 0.02 0.03',parent='flange'):
        path.write_text(f'<robot name="test"><link name="flange"/><link name="mount"/><link name="tcp"/>'
         f'<joint name="a" type="fixed"><parent link="{parent}"/><child link="mount"/>'
         f'<origin xyz="{xyz}" rpy="0 0 1.5707963267948966"/></joint>'
         f'<joint name="b" type="{joint_type}"><parent link="mount"/><child link="tcp"/>'
         '<origin xyz="0.1 0 0" rpy="0.1 0.2 0.3"/></joint></robot>')
    def test_relative_path_chain_rotation_and_units(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'arm.urdf';self.write(p)
            selected=dict(urdf='arm.urdf',flange_link='flange',tcp_link='tcp')
            r=resolve_urdf_tool(selected,d);T,name=parse_tool_frame(r)
            self.assertEqual(name,'tcp')
            np.testing.assert_allclose(T,transform([.01,.02,.03],[0,0,np.pi/2])@transform([.1,0,0],[.1,.2,.3]),atol=1e-12)
            np.testing.assert_allclose(r['xyz_mm'],[10,120,30],atol=1e-12)
            cfg={'tool_tcp':{'left':selected,'right':None}}
            parsed=validate_tool_config(cfg,d)
            self.assertIsNone(parsed['right']);self.assertEqual(cfg['tool_tcp']['left'],selected)
    def test_rejects_dynamic_invalid_or_disconnected_tcp(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'arm.urdf';s=dict(urdf=str(p),flange_link='flange',tcp_link='tcp')
            for kwargs in [dict(joint_type='revolute'),dict(parent='unknown'),dict(parent='tcp'),dict(xyz='nan 0 0'),dict(xyz='1 2')]:
                self.write(p,**kwargs)
                with self.assertRaises(ValueError):resolve_urdf_tool(s)
            self.write(p)
            with self.assertRaises(ValueError):resolve_urdf_tool({**s,'tcp_link':'missing'})
    def test_installed_config_and_display_match_urdf(self):
        config_path=VENDOR/'teleop/marvin_teleop/config.json'
        c=configuration(config_path);cad=json.loads((ROOT/'public/tianji-model.json').read_text())
        for side,letter in [('left','L'),('right','R')]:
            source=c['tool_tcp_source'][side]
            self.assertEqual(source['tcp_link'],f'grasp_center_{letter}')
            T,name=parse_tool_frame(c['tool_tcp'][side])
            expected=np.eye(4)
            for joint in [f'gripper_mount_{letter}',f'grasp_center_fixed_{letter}']:
                j=next(j for j in cad['joints'] if j['name']==joint)
                expected=expected@transform(j['xyz'],j['rpy'])
            np.testing.assert_allclose(T,expected,atol=1e-12)
            np.testing.assert_allclose(T[:3,:3],[[0,0,-1],[0,1,0],[1,0,0]],atol=1e-10)
            self.assertAlmostEqual(T[2,3],.202453)
            self.assertGreater(np.linalg.norm(T[:3,3]),.2)
    def test_changing_urdf_origin_changes_loaded_tool_without_json_edits(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'arm.urdf';self.write(p)
            s=dict(urdf=str(p),flange_link='flange',tcp_link='tcp')
            first,_=parse_tool_frame(resolve_urdf_tool(s))
            self.write(p,xyz='0.04 0.02 0.03')
            second,_=parse_tool_frame(resolve_urdf_tool(s))
            np.testing.assert_allclose(second[:3,3]-first[:3,3],[.03,0,0],atol=1e-12)

if __name__=='__main__':unittest.main()
