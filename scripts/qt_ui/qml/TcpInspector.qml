import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D
ApplicationWindow {
    id: root
    visible: true
    width: 1280; height: 900
    title: "天机 TCP 三维仿真 · P 点位置"
    color: "#101923"
    property bool translucent: true
    property int projectionTick: 0
    Timer { interval: 50; running: true; repeat: true; onTriggered: root.projectionTick++ }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 22; spacing: 12
        Label { text: "P 点 · 遥操作中心"; color: "#edf5ff"; font.pixelSize: 27; font.bold: true }
        Label { text: "独立几何仿真 · 使用当前 URDF / CAD · 不连接机器人"; color: "#9baec2"; font.pixelSize: 15 }
        RowLayout {
            spacing: 10
            Button { text: "左夹爪近看"; highlighted: tcpViewer.selected === "left"; onClicked: tcpViewer.select("left") }
            Button { text: "右夹爪近看"; highlighted: tcpViewer.selected === "right"; onClicked: tcpViewer.select("right") }
            Button { text: "整机位置"; highlighted: tcpViewer.selected === "all"; onClicked: tcpViewer.select("all") }
            Button { text: "复位视角"; onClicked: tcpViewer.reset() }
            Item { Layout.fillWidth: true }
            CheckBox { text: "半透明观察内部"; checked: true; onToggled: root.translucent = checked; palette.windowText: "#d9e7f5" }
        }
        Rectangle {
            Layout.fillWidth: true; Layout.fillHeight: true
            color: "#182431"; radius: 12; clip: true
            View3D {
                id: view; anchors.fill: parent
                environment: SceneEnvironment { backgroundMode: SceneEnvironment.Color; clearColor: "#182431"; antialiasingMode: SceneEnvironment.MSAA; antialiasingQuality: SceneEnvironment.High }
                camera: OrthographicCamera {
                    position: tcpViewer.cameraPosition; rotation: tcpViewer.cameraOrientation
                    horizontalMagnification: Math.max(1, view.height / tcpViewer.viewHeight)
                    verticalMagnification: Math.max(1, view.height / tcpViewer.viewHeight)
                    clipNear: .01; clipFar: 20
                }
                DirectionalLight { eulerRotation: Qt.vector3d(-30, -35, 0); brightness: 1.2; ambientColor: "#999999" }
                Repeater3D {
                    model: tcpViewer.parts
                    delegate: Model {
                        required property var modelData
                        geometry: modelData.mesh; position: modelData.position; rotation: modelData.orientation; scale: modelData.size
                        materials: DefaultMaterial {
                            diffuseColor: Qt.rgba(modelData.tint.x, modelData.tint.y, modelData.tint.z, 1)
                            opacity: root.translucent ? .27 : 1
                            cullMode: Material.NoCulling
                        }
                    }
                }
                Repeater3D {
                    model: tcpViewer.lines
                    delegate: Model {
                        required property var modelData
                        source: "#Cylinder"; position: modelData.position; rotation: modelData.orientation; scale: modelData.size
                        materials: DefaultMaterial { diffuseColor: modelData.color; lighting: DefaultMaterial.NoLighting }
                    }
                }
                Repeater3D {
                    model: tcpViewer.markers
                    delegate: Model {
                        required property var modelData
                        source: "#Sphere"; position: modelData.position; scale: Qt.vector3d(.000075,.000075,.000075)
                        materials: DefaultMaterial { diffuseColor: modelData.color; lighting: DefaultMaterial.NoLighting }
                    }
                }
            }
            MouseArea {
                anchors.fill: parent; cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                property real lastX; property real lastY
                onPressed: mouse => { lastX = mouse.x; lastY = mouse.y }
                onPositionChanged: mouse => { if (pressed) { tcpViewer.drag(mouse.x-lastX, mouse.y-lastY); lastX=mouse.x; lastY=mouse.y } }
                onWheel: wheel => tcpViewer.changeZoom(wheel.angleDelta.y > 0 ? 1.12 : .88)
            }
            Repeater {
                model: tcpViewer.markers
                delegate: Item {
                    required property var modelData
                    property vector3d screenPoint: { tcpViewer.revision; root.projectionTick; view.width; view.height; return view.mapFrom3DScene(modelData.position) }
                    x: screenPoint.x; y: screenPoint.y
                    Rectangle { x: -5; y: -5; width: 10; height: 10; radius: 5; color: "#50b5ff" }
                    Rectangle { x: -13; y: -13; width: 26; height: 26; radius: 13; color: "transparent"; border.width: 2; border.color: "#50b5ff" }
                    Rectangle { x: 15; y: -2; width: 45; height: 2; color: "#50b5ff" }
                    Rectangle {
                        x: 60; y: -22; width: label.width+24; height: 44; radius: 8; color: "#113654"; border.color: "#50b5ff"
                        Label { id: label; anchors.centerIn: parent; text: modelData.label; color: "#d9f0ff"; font.pixelSize: 18; font.bold: true }
                    }
                }
            }
            Label { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: 16; text: "拖动旋转 · 滚轮缩放 · 蓝色圆环始终标示 P 的投影位置"; color: "#a9bfd3"; font.pixelSize: 14 }
        }
        RowLayout {
            Label { text: "模拟夹爪开合"; color: "#d9e7f5"; font.pixelSize: 16 }
            Slider { id: opening; Layout.preferredWidth: 300; from: 0; to: 1; value: .65; onMoved: tcpViewer.setOpening(value) }
            Label { text: "P 固定在夹爪根部，不随开合移动"; color: "#94adc4"; font.pixelSize: 15 }
        }
        Label { text: "蓝色：P 点    金色：电机轴线    绿色：两侧夹指固定转轴"; color: "#d9e7f5"; font.pixelSize: 16 }
        Label { text: "P 距机械臂侧连接面约 142.23 mm；距电机侧连接面约 89.53 mm。尺寸来自当前 CAD。"; color: "#94adc4"; font.pixelSize: 15 }
    }
}
