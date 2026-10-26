#!/usr/bin/env python3
"""Render an offline TCP diagram from the current assembled URDF and App meshes."""
import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication,QSurfaceFormat
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest
from shiboken6 import delete
from qt_ui.visuals import KuavoScene,transform

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path)
    output=parser.parse_args().output;output.parent.mkdir(parents=True,exist_ok=True)
    fmt=QSurfaceFormat();fmt.setVersion(3,3);fmt.setProfile(QSurfaceFormat.CoreProfile);QSurfaceFormat.setDefaultFormat(fmt)
    app=QGuiApplication([]);engine=QQmlApplicationEngine();scenes=[];panels=[]
    robot=ET.parse(ROOT/'teleop_control/assets/kuavo/full_robot_with_grippers.urdf').getroot()
    joints={j.get('name'):j for j in robot.findall('joint')}
    width,height=540,640
    for side,letter,yaw in [('left','L',math.pi+.25),('right','R',math.pi+1.25)]:
        scene=KuavoScene(ROOT);wrist=next(p.matrix.copy() for n,p in scene.by_link if n==f'zarm_{letter.lower()}7_link')
        parts=[p for n,p in scene.by_link if n==f'zarm_{letter.lower()}7_link' or n=='kuavo_tool_flange_'+letter.lower() or n.startswith('kuavo_tool_gripper_'+letter)]
        for p in parts:p.update_matrix(np.linalg.inv(wrist)@p.matrix)
        scene.parts_value=parts;scene.zoom=6.3;scene.yaw=yaw;scene.pitch=.12;scene.update_camera()
        scene.camera_matrix[:3,3]+=np.array([0,0,-.98])
        def mount(name):
            node=joints[name].find('origin')
            return transform([float(v) for v in node.get('xyz').split()],[float(v) for v in node.get('rpy').split()])
        grip=mount('kuavo_tool_flange_mount_'+letter.lower())@mount('kuavo_tool_gripper_mount_'+letter)
        tcp=grip@mount('kuavo_tool_grasp_center_fixed_'+letter)
        camera=np.linalg.inv(scene.camera_matrix);scale=min(height*.51,width*.35)*scene.zoom
        def project(point):
            p=camera@np.r_[point,1]
            return [round(float(width/2+p[0]*scale),3),round(float(height/2-p[1]*scale),3)]
        axes=[project(tcp[:3,3]+tcp[:3,i]*.042) for i in range(3)]
        panels.append(dict(tcp=project(tcp[:3,3]),origin=project(grip[:3,3]),axes=axes,
                           wrist_tcp_mm=np.round(tcp[:3,3]*1000,6).tolist()))
        scenes.append(scene);engine.rootContext().setContextProperty('scene'+letter,scene)
    engine.rootContext().setContextProperty('annotations',panels)
    qml='''import QtQuick
import QtQuick3D
Window {
 id:win;width:1180;height:900;flags:Qt.FramelessWindowHint | Qt.BypassWindowManagerHint;visible:true;color:"#f3f5f8"
 Text {x:40;y:26;text:"乐聚夹爪 · 当前 TCP 位置";font.pixelSize:26;font.bold:true;color:"#172b45"}
 Text {x:40;y:66;text:"按当前装配 URDF 绘制  ·  夹爪半开示意  ·  红点为穿透标注";font.pixelSize:14;color:"#586779"}
 component Panel: Rectangle {
  required property var scene;required property var note;required property string title
  width:540;height:692;radius:14;color:"white";border.color:"#d6dde7"
  Text {x:18;y:14;text:title;font.pixelSize:17;font.bold:true;color:"#253c58"}
  Item {id:stage;x:0;y:44;width:540;height:640
   View3D {anchors.fill:parent
    environment:SceneEnvironment {backgroundMode:SceneEnvironment.Transparent;antialiasingMode:SceneEnvironment.MSAA;antialiasingQuality:SceneEnvironment.High;tonemapMode:SceneEnvironment.TonemapModeNone}
    camera:OrthographicCamera {position:scene.cameraPosition;rotation:scene.cameraOrientation;horizontalMagnification:Math.min(stage.height*.51,stage.width*.35)*scene.zoomValue;verticalMagnification:horizontalMagnification;clipNear:.01;clipFar:100}
    Repeater3D {model:scene.parts;delegate:Model {
     required property var modelData;geometry:modelData.mesh;position:modelData.position;rotation:modelData.orientation;scale:modelData.size
     materials:CustomMaterial {property vector3d robotColor:modelData.tint;shadingMode:CustomMaterial.Unshaded;cullMode:Material.NoCulling;vertexShader:"robot.vert";fragmentShader:"robot.frag"}
    }}
   }
   Canvas {anchors.fill:parent;onPaint:{
    const c=getContext("2d");c.clearRect(0,0,width,height);
    const p=note.tcp,o=note.origin,colors=["#df563e","#23a167","#397ce0"];
    c.lineWidth=1.5;c.strokeStyle="#3f83b9";c.setLineDash([5,4]);c.beginPath();c.moveTo(o[0],o[1]);c.lineTo(p[0],p[1]);c.stroke();c.setLineDash([]);
    c.fillStyle="#3f83b9";c.beginPath();c.arc(o[0],o[1],4,0,Math.PI*2);c.fill();
    c.font="13px sans-serif";c.fillText("O · 夹爪模型原点",o[0]+12,o[1]-4);
    note.axes.forEach((a,i)=>{const dx=a[0]-p[0],dy=a[1]-p[1],len=Math.hypot(dx,dy);c.strokeStyle=colors[i];c.fillStyle=colors[i];c.lineWidth=2.5;c.beginPath();c.moveTo(p[0],p[1]);c.lineTo(a[0],a[1]);c.stroke();if(len>1){const ux=dx/len,uy=dy/len;c.beginPath();c.moveTo(a[0],a[1]);c.lineTo(a[0]-8*ux+4*uy,a[1]-8*uy-4*ux);c.lineTo(a[0]-8*ux-4*uy,a[1]-8*uy+4*ux);c.closePath();c.fill();}c.fillText(["+X","+Y","+Z"][i],a[0]+6,a[1]-5);});
    c.fillStyle="#d82c42";c.strokeStyle="white";c.lineWidth=2;c.beginPath();c.arc(p[0],p[1],7,0,Math.PI*2);c.fill();c.stroke();
    c.strokeStyle="#d82c42";c.lineWidth=1.5;c.beginPath();c.moveTo(p[0]+9,p[1]-8);c.lineTo(p[0]+62,p[1]-47);c.lineTo(p[0]+135,p[1]-47);c.stroke();c.fillStyle="#b81e35";c.font="bold 15px sans-serif";c.fillText("TCP 固定中心",p[0]+65,p[1]-56);
   }}
  }
 }
 Panel {x:40;y:106;scene:sceneL;note:annotations[0];title:"左夹爪 · 正面斜视"}
 Panel {x:600;y:106;scene:sceneR;note:annotations[1];title:"右夹爪 · 侧面斜视"}
 Text {x:40;y:819;text:"相对夹爪模型原点 O：X = 0    Y = 0    Z = 142.226 mm（左右相同）";font.pixelSize:16;color:"#243c57"}
 Text {x:40;y:850;text:"沿用天机固定 TCP：电机轴线与两侧夹指固定转轴平面的交点；不随开合变化，尚非实物标定值。";font.pixelSize:14;color:"#586779"}
}'''
    warnings=[];engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
    for shader in ('robot.vert','robot.frag'):
        qml=qml.replace(chr(34)+shader+chr(34),json.dumps(QUrl.fromLocalFile(str(ROOT/'scripts/qt_ui/qml'/shader)).toString()))
    qml_path=output.with_suffix('.qml');qml_path.write_text(qml)
    engine.load(QUrl.fromLocalFile(str(qml_path.resolve())))
    if not engine.rootObjects():raise RuntimeError(warnings)
    window=engine.rootObjects()[0];QTest.qWait(500)
    if warnings:raise RuntimeError(warnings)
    assert window.grabWindow().save(str(output))
    output.with_suffix('.json').write_text(json.dumps(dict(source='teleop_control/assets/kuavo/full_robot_with_grippers.urdf',panels=panels),indent=2)+'\n')
    window.close();delete(engine)
    print(output)


if __name__=='__main__':main()
