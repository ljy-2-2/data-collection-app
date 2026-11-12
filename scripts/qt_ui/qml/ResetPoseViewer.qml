import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D

ApplicationWindow {
    id: win; width: 1320; height: 900; visible: true
    title: s.symmetric ? "天机双臂对称复位姿态 · 离线仿真" : "天机右臂复位姿态 · 离线仿真"
    color: "#edf1f5"
    property var s: resetPreview.state
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 18; spacing: 10
        Label { text: s.symmetric ? "双臂对称 L 形复位姿态预览" : "右臂 L 形复位姿态预览"; font.pixelSize: 24; font.bold: true; color: "#26364a" }
        Label { text: (s.symmetric ? "右臂蓝色 · 左臂绿色 · 左右镜像对称" : "右臂蓝色 · 左臂灰色并保持快照姿态") + " · 离线预览，不执行实机运动"; color: "#566579" }
        Label { text: s.source; color: "#566579"; Layout.fillWidth: true; wrapMode: Text.Wrap }
        RowLayout {
            Button { text: "原始姿态"; highlighted: s.selected === "original"; onClicked: resetPreview.select("original") }
            Button { text: "候选复位姿态"; highlighted: s.selected === "candidate"; onClicked: resetPreview.select("candidate") }
            Button { text: "原角度取整"; onClicked: resetPreview.rounded() }
            Button { text: "恢复 L 形方案"; onClicked: resetPreview.restore() }
            Item { Layout.fillWidth: true }
            Button { text: "侧面"; onClicked: resetPreview.view("side") }
            Button { text: "正面"; onClicked: resetPreview.view("front") }
            Button { text: "立体"; onClicked: resetPreview.view("perspective") }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 300
            Component.onCompleted: robotScene.prepareView()
            View3D {
                id: view; anchors.fill: parent
                environment: SceneEnvironment { backgroundMode: SceneEnvironment.Color; clearColor: "#e6ebf2"; antialiasingMode: SceneEnvironment.MSAA; antialiasingQuality: SceneEnvironment.High }
                camera: OrthographicCamera {
                    position: robotScene.cameraPosition; rotation: robotScene.cameraOrientation
                    horizontalMagnification: Math.max(1, Math.min(view.height*.51,view.width*.35)*robotScene.zoomValue)
                    verticalMagnification: Math.max(1, Math.min(view.height*.51,view.width*.35)*robotScene.zoomValue)
                    clipNear: .01; clipFar: 100
                }
                Repeater3D {
                    model: robotScene.parts
                    delegate: Model {
                        required property var modelData
                        geometry: modelData.mesh; position: modelData.position; rotation: modelData.orientation; scale: modelData.size
                        materials: CustomMaterial {
                            property vector3d robotColor: modelData.tint
                            shadingMode: CustomMaterial.Unshaded; cullMode: Material.NoCulling
                            vertexShader: "robot.vert"; fragmentShader: "robot.frag"
                        }
                    }
                }
            }
            RobotOrbitArea { anchors.fill: parent; scene: robotScene }
            Label { anchors.left: parent.left; anchors.top: parent.top; anchors.margins: 10; text: "左键旋转 · 右键平移 · 滚轮缩放"; color: "#566579" }
        }
        Label { text: s.summary; color: "#26364a"; font.pixelSize: 16 }
        Label { text: s.difference; color: "#566579" }
        RowLayout {
            Layout.fillWidth: true
            Repeater {
                model: 7
                delegate: ColumnLayout {
                    required property int index
                    Layout.fillWidth: true
                    Label { text: "J"+(index+1); font.bold: true; color: "#26364a" }
                    Label { text: "右原始 "+Number(s.original[index]).toFixed(3)+"°"; color: "#566579" }
                    Label { text: "右候选 "+Number(s.candidate[index]).toFixed(0)+"°"; color: "#1768b1"; font.pixelSize: 18 }
                    Label { visible: s.symmetric; text: "左候选 "+Number(s.leftCandidate[index]).toFixed(0)+"°"; color: "#16794f"; font.pixelSize: 18 }
                    RowLayout {
                        Button { text: "−1°"; onClicked: resetPreview.adjust(index, -1) }
                        Button { text: "+1°"; onClicked: resetPreview.adjust(index, 1) }
                    }
                }
            }
        }
        Label { text: "角度为厂商 J1～J7 坐标。L 形按上臂和前臂主体纵轴的侧面投影衡量，保留 URDF 中 18 mm 的横向偏置；此预览不是实机运动轨迹。"; color: "#566579"; Layout.fillWidth: true; wrapMode: Text.Wrap }
    }
}
