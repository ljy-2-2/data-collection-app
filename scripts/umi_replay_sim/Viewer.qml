import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D
import "../qt_ui/qml" as UI

ApplicationWindow {
    id:win;width:1280;height:820;visible:true
    title:"天机 UMI 完整位姿离线仿真"
    color:"#edf1f5"
    palette.window:"#edf1f5";palette.base:"#ffffff";palette.text:"#26364a"
    palette.button:"#ffffff";palette.buttonText:"#26364a";palette.highlight:"#b98b00"
    property var s:backend.state
    ColumnLayout {
        anchors.fill:parent;anchors.margins:18;spacing:10
        Label {text:"UMI 回放仿真 · 位置与朝向均约束";font.pixelSize:23;font.bold:true;color:"#26364a"}
        Label {text:s.initial+"   |   每条记录独立从同一起点开始   |   无实机控制接口";color:"#566579"}
        ComboBox {Layout.fillWidth:true;model:backend.labels;currentIndex:s.index;onActivated:backend.select(currentIndex)}
        Rectangle {
            Layout.fillWidth:true;implicitHeight:info.implicitHeight+20;radius:8
            color:s.complete?"#e2f4e9":"#fff0e5";border.color:s.complete?"#aacfb7":"#e5c5a8"
            Column {id:info;x:12;y:10;width:parent.width-24;spacing:5
                Label {width:parent.width;text:s.status;font.bold:true;color:s.complete?"#187749":"#9a4824"}
                Label {width:parent.width;text:s.error;visible:!!text;wrapMode:Text.Wrap;font.pixelSize:12;color:"#773e2b"}
            }
        }
        Item {
            Layout.fillWidth:true;Layout.fillHeight:true;Layout.minimumHeight:250
            Component.onCompleted:robotScene.prepareView()
            View3D {
                id:view;anchors.fill:parent
                environment:SceneEnvironment {backgroundMode:SceneEnvironment.Color;clearColor:"#e6ebf2";antialiasingMode:SceneEnvironment.MSAA;antialiasingQuality:SceneEnvironment.High;tonemapMode:SceneEnvironment.TonemapModeNone}
                camera:OrthographicCamera {
                    position:robotScene.cameraPosition;rotation:robotScene.cameraOrientation
                    horizontalMagnification:Math.max(1,Math.min(view.height*.51,view.width*.35)*robotScene.zoomValue)
                    verticalMagnification:Math.max(1,Math.min(view.height*.51,view.width*.35)*robotScene.zoomValue)
                    clipNear:.01;clipFar:100
                }
                Repeater3D {
                    model:robotScene.parts
                    delegate:Model {
                        required property var modelData
                        geometry:modelData.mesh;position:modelData.position;rotation:modelData.orientation;scale:modelData.size
                        materials:CustomMaterial {
                            property vector3d robotColor:modelData.tint
                            shadingMode:CustomMaterial.Unshaded;cullMode:Material.NoCulling
                            vertexShader:"../qt_ui/qml/robot.vert";fragmentShader:"../qt_ui/qml/robot.frag"
                        }
                    }
                }
                Repeater3D {
                    model:backend.targetFrames
                    delegate:Node {
                        required property var modelData
                        position:modelData.position;rotation:modelData.orientation
                        Model {source:"#Sphere";scale:Qt.vector3d(.00014,.00014,.00014);materials:PrincipledMaterial {baseColor:"#ffcc00";lighting:PrincipledMaterial.NoLighting}}
                        Model {source:"#Cylinder";position:Qt.vector3d(.04,0,0);eulerRotation.z:-90;scale:Qt.vector3d(.00005,.0008,.00005);materials:PrincipledMaterial {baseColor:"#e93838";lighting:PrincipledMaterial.NoLighting}}
                        Model {source:"#Cylinder";position:Qt.vector3d(0,.04,0);scale:Qt.vector3d(.00005,.0008,.00005);materials:PrincipledMaterial {baseColor:"#20a455";lighting:PrincipledMaterial.NoLighting}}
                        Model {source:"#Cylinder";position:Qt.vector3d(0,0,.04);eulerRotation.x:90;scale:Qt.vector3d(.00005,.0008,.00005);materials:PrincipledMaterial {baseColor:"#2674f0";lighting:PrincipledMaterial.NoLighting}}
                    }
                }
            }
            UI.RobotOrbitArea {anchors.fill:parent;scene:robotScene}
            Label {anchors.left:parent.left;anchors.top:parent.top;anchors.margins:10;text:"黄点：目标 TCP    红 / 绿 / 蓝：目标 X / Y / Z 轴\n左键旋转 · 右键平移 · 滚轮缩放";color:"#475a70";font.pixelSize:12}
            Button {anchors.right:parent.right;anchors.top:parent.top;text:"重置视角";onClicked:robotScene.resetView()}
        }
        Label {Layout.fillWidth:true;wrapMode:Text.Wrap;text:s.complete?"模型按通过验证的关节曲线显示；播放速度已受关节速度、加速度上限约束。":"失败记录只显示已求解前缀，之后模型保持不动；目标坐标轴继续展示原始相对轨迹。前缀未通过整段检查，不可用于实机。";color:"#586879";font.pixelSize:12}
        RowLayout {
            Layout.fillWidth:true
            Button {text:s.playing?"暂停":"播放仿真";enabled:s.duration>0;onClicked:backend.toggle()}
            Slider {Layout.fillWidth:true;from:0;to:Math.max(.001,s.duration);value:s.time;onMoved:backend.seek(value)}
            Label {text:s.time.toFixed(2)+" / "+s.duration.toFixed(2)+" s";Layout.preferredWidth:130;color:"#475a70"}
        }
    }
}
