import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import Collection.Native 1.0

Card {
    id: root
    property bool tianji: true
    property bool active: false
    property bool locked: false
    property var status: ({})
    property var previewStatus: ({})
    property string layoutKey: tianji ? "tianjiCameraLayout" : "g2CameraLayout"
    property bool swapped: backend.state[layoutKey] === "right-center-left"
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
    Row {
        anchors.right: parent.right;anchors.rightMargin: 12;y: 9;spacing: 6
        Select {
            objectName: "teleopCameraLayout"
            width: Math.min(178,root.width-140)
            model: ["左腕 · 主 · 右腕","右腕 · 主 · 左腕"]
            currentIndex: root.swapped ? 1 : 0
            Accessible.name: "相机画面排列"
            onActivated: backend.setValue(root.layoutKey,currentIndex===0?"left-center-right":"right-center-left")
        }
        Btn {
            visible: root.tianji;objectName: "teleopCameraImport"
            width: 30;height: 34;glyph: "Settings2";ghost: true
            enabled: !root.locked;Accessible.name: "导入相机授权配置"
            ToolTip.visible: hovered;ToolTip.text: "导入相机授权配置"
            onClicked: credentialsDialog.open()
        }
    }
    Item {
        id: images
        objectName: "teleopCameraImages"
        x: 10;y: 61;width: parent.width-20;height: 302
        Repeater {
            // Camera identity stays fixed when moving a tile. Display order
            // must never relabel a stream, reconnect it, or change recording IDs.
            model: ["left","center","right"]
            Rectangle {
                id: tile
                required property string modelData
                property string role: modelData
                property int slot: role==="center" ? 1 : role==="left" ? (root.swapped?2:0) : (root.swapped?0:2)
                objectName: "teleopCameraTile-"+role
                x: slot*(width+6);width: (images.width-12)/3;height: images.height
                radius: 8;color: "black";border.color: Theme.line;clip: true
                CameraItem {
                    id: camera;objectName: "teleopCamera-"+tile.role
                    anchors.fill: parent;anchors.margins: 1;source: root.cameraSource(tile.role)
                }
                Txt {anchors.top: parent.top;anchors.right: parent.right;anchors.margins: 7;text: camera.online?"LIVE":"--";font.pixelSize: 9;color: camera.online?Theme.green:"#8793a5"}
                Rectangle {
                    anchors.centerIn: parent;width: Math.min(parent.width-8,label.implicitWidth+14);height: 26
                    radius: 5;color: camera.online?"#99000000":"#20252d"
                    Txt {id: label;objectName: "teleopCameraLabel-"+tile.role;anchors.centerIn: parent;width: Math.min(implicitWidth,parent.width-6);text: ({left:"左腕部相机",center:"主相机",right:"右腕部相机"})[tile.role];font.pixelSize: 12;color: "white";font.weight: Font.DemiBold}
                }
                Txt {visible: !camera.online;x: 4;y: parent.height/2+21;width: parent.width-8;text: "等待相机画面";font.pixelSize: 10;color: "#8793a5";horizontalAlignment: Text.AlignHCenter;wrapMode: Text.Wrap}
            }
        }
        Rectangle {
            visible: root.tianji && !!root.status.camera_error
            anchors.bottom: parent.bottom;width: parent.width;height: Math.min(94,errorText.implicitHeight+16)
            radius: 6;color: "#e820252d";clip: true
            Txt {id: errorText;x: 8;y: 8;width: parent.width-16;text: root.status.camera_error||"";font.pixelSize: 11;color: "#ffb4a9";wrapMode: Text.Wrap;elide: Text.ElideNone}
        }
    }
    FileDialog {
        id: credentialsDialog
        objectName: "teleopCameraFileDialog"
        title: "导入相机授权配置（保留 JSON 对应的证书文件）"
        nameFilters: ["相机配置 (*.json)"]
        onAccepted: backend.importTianjiCameraCredentials(selectedFile)
    }
}
