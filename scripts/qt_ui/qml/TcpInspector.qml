import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D
ApplicationWindow {
    id: root
    visible: true
    width: 1280; height: 900
    title: "天机 Marvin M6 · 夹爪原点与 XYZ 正方向"
    color: "#101923"
    property bool translucent: true
    property bool showModelAxes: true
    property int projectionTick: 0
    Timer { interval: 50; running: true; repeat: true; onTriggered: root.projectionTick++ }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 22; spacing: 12
        Label { text: "夹爪坐标 · O 模型原点 / P 末端控制中心（TCP）"; color: "#edf5ff"; font.pixelSize: 27; font.bold: true }
        Label { text: "坐标原点与方向直接读取当前 URDF · 箭头表示各局部坐标轴的正方向"; color: "#9baec2"; font.pixelSize: 15 }
        RowLayout {
            spacing: 10
            Button { text: "左夹爪近看"; highlighted: tcpViewer.selected === "left"; onClicked: tcpViewer.select("left") }
            Button { text: "右夹爪近看"; highlighted: tcpViewer.selected === "right"; onClicked: tcpViewer.select("right") }
            Button { text: "整机位置"; highlighted: tcpViewer.selected === "all"; onClicked: tcpViewer.select("all") }
            Button { text: "复位视角"; onClicked: tcpViewer.reset() }
            Item { Layout.fillWidth: true }
            CheckBox { text: "显示 O 坐标轴"; checked: true; onToggled: root.showModelAxes = checked; palette.windowText: "#d9e7f5" }
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
            Canvas {
                id: axisOverlay
                anchors.fill: parent
                property int revision: tcpViewer.revision
                property int projectionTick: root.projectionTick
                property bool showModelAxes: root.showModelAxes
                onRevisionChanged: requestPaint()
                onProjectionTickChanged: requestPaint()
                onShowModelAxesChanged: requestPaint()
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                onPaint: {
                    const c = getContext("2d"); c.clearRect(0, 0, width, height);
                    tcpViewer.axes.forEach(a => {
                        if (a.frame === "O" && !root.showModelAxes) return;
                        const p = view.mapFrom3DScene(a.origin), q = view.mapFrom3DScene(a.position);
                        const dx = q.x-p.x, dy = q.y-p.y, length = Math.hypot(dx,dy);
                        c.strokeStyle = a.color; c.fillStyle = a.color; c.lineWidth = 3;
                        c.beginPath(); c.moveTo(p.x,p.y); c.lineTo(q.x,q.y); c.stroke();
                        if (length > 1) {
                            const ux = dx/length, uy = dy/length;
                            c.beginPath(); c.moveTo(q.x,q.y);
                            c.lineTo(q.x-12*ux+5*uy,q.y-12*uy-5*ux);
                            c.lineTo(q.x-12*ux-5*uy,q.y-12*uy+5*ux); c.closePath(); c.fill();
                        }
                    });
                }
            }
            Repeater {
                model: tcpViewer.axes
                delegate: Label {
                    required property var modelData
                    visible: modelData.frame !== "O" || root.showModelAxes
                    property vector3d screenPoint: { tcpViewer.revision; root.projectionTick; view.width; view.height; return view.mapFrom3DScene(modelData.position) }
                    property vector3d screenOrigin: { tcpViewer.revision; root.projectionTick; view.width; view.height; return view.mapFrom3DScene(modelData.origin) }
                    property real axisLength: Math.max(1, Math.hypot(screenPoint.x-screenOrigin.x, screenPoint.y-screenOrigin.y))
                    x: screenPoint.x + (screenPoint.x-screenOrigin.x)/axisLength*(width/2+10) - width/2
                    y: screenPoint.y + (screenPoint.y-screenOrigin.y)/axisLength*(height/2+10) - height/2
                    text: modelData.label; color: modelData.color; font.bold: true; font.pixelSize: 16
                    padding: 4
                    background: Rectangle { color: "#d9182431"; radius: 4 }
                }
            }
            Repeater {
                model: tcpViewer.markers
                delegate: Item {
                    required property var modelData
                    property vector3d screenPoint: { tcpViewer.revision; root.projectionTick; view.width; view.height; return view.mapFrom3DScene(modelData.position) }
                    x: screenPoint.x; y: screenPoint.y
                    Rectangle { x: -5; y: -5; width: 10; height: 10; radius: 5; color: modelData.color }
                    Rectangle { x: -13; y: -13; width: 26; height: 26; radius: 13; color: "transparent"; border.width: 2; border.color: modelData.color }
                    Rectangle { x: modelData.labelRight ? 15 : -60; y: -2; width: 45; height: 2; color: modelData.color }
                    Rectangle {
                        x: modelData.labelRight ? 60 : -width-60; y: -22; width: label.width+24; height: 44; radius: 8; color: "#113654"; border.color: modelData.color
                        Label { id: label; anchors.centerIn: parent; text: modelData.label; color: "#d9f0ff"; font.pixelSize: 18; font.bold: true }
                    }
                }
            }
            Label { anchors.left: parent.left; anchors.bottom: parent.bottom; anchors.margins: 16; text: "拖动旋转 · 滚轮缩放 · 圆环和箭头为穿透标注，旋转视角可查看三维方向"; color: "#a9bfd3"; font.pixelSize: 14 }
        }
        RowLayout {
            Label { text: "模拟夹爪开合"; color: "#d9e7f5"; font.pixelSize: 16 }
            Slider { id: opening; Layout.preferredWidth: 300; from: 0; to: 1; value: .65; onMoved: tcpViewer.setOpening(value) }
            Label { text: "P 固定在夹爪根部，不随开合移动"; color: "#94adc4"; font.pixelSize: 15 }
        }
        Label { text: "蓝圈 P：TCP 原点    金圈 O：夹爪模型原点（与法兰中心重合）    正方向：+X 红 / +Y 绿 / +Z 蓝"; color: "#d9e7f5"; font.pixelSize: 15 }
        Label { text: tcpViewer.controlDescription; color: "#d9e7f5"; font.pixelSize: 14; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        Label { text: "TCP +X：沿指尖伸出    +Y：沿两指张开方向    +Z：右手法向；O 与 TCP 的坐标轴朝向不同。"; color: "#d9e7f5"; font.pixelSize: 14 }
        Label { text: "P 位于电机轴线与两侧夹指固定转轴所在平面的交点；位置来自当前 URDF / CAD，尚非实测标定值。"; color: "#94adc4"; font.pixelSize: 14 }
    }
}
