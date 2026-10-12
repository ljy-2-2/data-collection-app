import QtQuick
import QtQuick3D
Item {
    id: root
    View3D {
        id: view
        anchors.fill: parent
        environment: SceneEnvironment {backgroundMode: SceneEnvironment.Transparent;clearColor: Theme.stage;antialiasingMode: SceneEnvironment.MSAA;antialiasingQuality: SceneEnvironment.High;tonemapMode: SceneEnvironment.TonemapModeNone}
        camera: OrthographicCamera {
            position: robotScene.cameraPosition;rotation: robotScene.cameraOrientation
            horizontalMagnification: Math.max(1,view.height*.51*robotScene.zoomValue)
            verticalMagnification: Math.max(1,view.height*.51*robotScene.zoomValue)
            clipNear: .01;clipFar: 100
        }
        Repeater3D {
            model: robotScene.parts
            delegate: Model {
                required property var modelData
                geometry: modelData.mesh
                position: modelData.position
                rotation: modelData.orientation
                scale: modelData.size
                materials: CustomMaterial {
                    property vector3d robotColor: Qt.vector3d(Math.min(1,modelData.tint.x*(Theme.dark?1.15:1)),Math.min(1,modelData.tint.y*(Theme.dark?1.15:1)),Math.min(1,modelData.tint.z*(Theme.dark?1.15:1)))
                    shadingMode: CustomMaterial.Unshaded
                    cullMode: Material.NoCulling
                    vertexShader: "robot.vert"
                    fragmentShader: "robot.frag"
                }
            }
        }
    }
    MouseArea {
        anchors.fill: parent
        property real lastX: 0;property real lastY: 0
        cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
        onPressed: mouse=>{lastX=mouse.x;lastY=mouse.y}
        onPositionChanged: mouse=>{if(pressed){robotScene.drag(mouse.x-lastX,mouse.y-lastY);lastX=mouse.x;lastY=mouse.y}}
        onWheel: wheel=>robotScene.changeZoom(wheel.angleDelta.y>0?1.12:.88)
    }
    Rectangle {
        anchors.right: parent.right;anchors.top: parent.top;anchors.margins: 8;width: controls.width+4;height: 32;radius: 7;color: Theme.surface;border.color: Theme.line
        Row {id: controls;x: 2;y: 2;spacing: 1
            Btn {text: "−";width: 28;height: 28;ghost: true;onClicked: robotScene.changeZoom(.88)}
            Btn {text: "+";width: 28;height: 28;ghost: true;onClicked: robotScene.changeZoom(1.12)}
            Btn {text: "↺";width: 28;height: 28;ghost: true;onClicked: robotScene.resetView()}
        }
    }
}
