import QtQuick
import QtQuick.Controls
import Collection.Native 1.0

Card {
    id: root
    property bool tianji: true
    property bool active: false
    property bool locked: false
    property var status: ({})
    property var previewStatus: ({})
    objectName: "teleopCameraCard"

    function cameraId(role) {
        if (tianji) return {left:"usb_4",center:"usb_1",right:"usb_2"}[role]
        return {left:"hand_left_color",center:"head_color",right:"hand_right_color"}[role]
    }
    function cameraSource(role) {
        var id = cameraId(role)
        var entry = ((tianji ? status : previewStatus).cameras || []).find(c => c.id === id)
        var streaming = tianji && ["connecting","starting","streaming","cooldown","resuming","recovering"].indexOf(status.camera_phase) >= 0
        return active && ((entry && entry.available) || streaming)
            ? backend.baseUrl + (tianji ? "/tianji/cameras/"+id+"/stream" : "/cameras/"+id+"/frame") : ""
    }
    Heading {width: parent.width;title: "相机画面";height: 51}
    Item {
        id: images
        objectName: "teleopCameraImages"
        x: 10;y: 61;width: parent.width-20;height: 302
        readonly property real mainWidth: (width-6)*2/3
        readonly property real wristWidth: (width-6)/3
        Repeater {
            // Main camera fills the left; left/right wrists stack on the right.
            // Roles retain their stream IDs and recording identities.
            model: ["left","center","right"]
            Rectangle {
                id: tile
                required property string modelData
                property string role: modelData
                objectName: "teleopCameraTile-"+role
                x: role==="center" ? 0 : images.mainWidth+6
                y: role==="right" ? (images.height+6)/2 : 0
                width: role==="center" ? images.mainWidth : images.wristWidth
                height: role==="center" ? images.height : (images.height-6)/2
                radius: 8;color: "black";border.color: Theme.line;clip: true
                CameraItem {
                    id: camera;objectName: "teleopCamera-"+tile.role
                    anchors.fill: parent;anchors.margins: 1;source: root.cameraSource(tile.role)
                }
                Txt {anchors.top: parent.top;anchors.right: parent.right;anchors.margins: 7;text: camera.online?"LIVE":"--";font.pixelSize: 9;color: camera.online?Theme.green:"#8793a5"}
                Rectangle {
                    visible: !camera.hasFrame
                    anchors.centerIn: parent;width: Math.min(parent.width-8,label.implicitWidth+14);height: 26
                    radius: 5;color: camera.online?"#99000000":"#20252d"
                    Txt {id: label;objectName: "teleopCameraLabel-"+tile.role;anchors.centerIn: parent;width: Math.min(implicitWidth,parent.width-6);text: ({left:"左腕部相机",center:"主相机",right:"右腕部相机"})[tile.role];font.pixelSize: 12;color: "white";font.weight: Font.DemiBold}
                }
                Txt {objectName: "teleopCameraStale-"+tile.role;visible: camera.hasFrame && !camera.online;anchors.bottom: parent.bottom;anchors.horizontalCenter: parent.horizontalCenter;anchors.bottomMargin: 6;text: "画面已暂停";font.pixelSize: 10;color: "#ffb4a9"}
            }
        }
    }
}
