import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Collection.Native 1.0

Card {
    id: root
    property bool tianji: true
    property bool active: false
    property bool locked: false
    property var status: ({})
    property var previewStatus: ({})
    objectName: "teleopCameraCard"
    // Keep delegates/readers stable when only timestamps change during polling.
    readonly property string cameraKeys: tianji
        ? (status.cameras || []).filter(c => c.connected !== false).map(c => c.id)
            .sort((a, b) => displayOrder(a)-displayOrder(b) || (a < b ? -1 : a > b ? 1 : 0)).join("|")
        : "hand_left_color|head_color|hand_right_color"
    readonly property var cameraIds: cameraKeys ? cameraKeys.split("|") : []
    readonly property int controlsHeight: tianji ? 113 : 0
    function role(id) {
        return ({usb_4:"left",usb_1:"center",usb_2:"right",hand_left_color:"left",head_color:"center",hand_right_color:"right"})[id] || id
    }
    function displayOrder(id) {
        var order = ["left", "center", "right"].indexOf(role(id))
        return order < 0 ? 3 : order
    }
    function cameraLabel(id) {
        var entry = ((tianji ? status : previewStatus).cameras || []).find(c => c.id === id)
        return (entry && entry.label) || ({left:"左腕部相机",center:"主相机",right:"右腕部相机"})[role(id)] || id
    }
    function cameraSource(id) {
        var entry = ((tianji ? status : previewStatus).cameras || []).find(c => c.id === id)
        return active && ((entry && entry.available) || (tianji && entry && entry.connected !== false))
            ? backend.baseUrl + (tianji ? "/tianji/cameras/"+id+"/stream" : "/cameras/"+id+"/frame") : ""
    }
    Heading {width: parent.width;title: "相机画面";height: 51}
    Txt {anchors.top: parent.top;anchors.right: parent.right;anchors.margins: 16;text: root.tianji && root.status.camera_preview_mode === "snapshot" ? "逐路刷新" : "";font.pixelSize: 11;color: "#8793a5"}
    Item {
        id: images
        objectName: "teleopCameraImages"
        x: 10;y: 61;width: parent.width-20;height: 302+root.controlsHeight
        readonly property int columns: root.cameraIds.length <= 3
            ? Math.max(1, root.cameraIds.length) : Math.ceil(Math.sqrt(root.cameraIds.length))
        readonly property int rows: Math.max(1, Math.ceil(root.cameraIds.length / columns))
        readonly property real mainWidth: (width-6)*2/3
        readonly property real wristWidth: (width-6)/3
        Txt {anchors.centerIn: parent;visible: root.cameraIds.length === 0;text: root.status.camera_error || "等待识别已连接的相机";width: parent.width-20;wrapMode: Text.Wrap;font.pixelSize: 12;horizontalAlignment: Text.AlignHCenter}
        Repeater {
            model: root.cameraIds
            Rectangle {
                id: tile
                required property string modelData
                required property int index
                property string role: root.role(modelData)
                objectName: "teleopCameraTile-"+role
                x: root.tianji ? (index % images.columns)*(width+6) : (role==="center" ? 0 : images.mainWidth+6)
                y: root.tianji ? Math.floor(index/images.columns)*(height+6) : (role==="right" ? (images.height+6)/2 : 0)
                width: root.tianji ? (images.width-6*(images.columns-1))/images.columns : (role==="center" ? images.mainWidth : images.wristWidth)
                height: root.tianji ? (images.height-6*(images.rows-1))/images.rows : (role==="center" ? images.height : (images.height-6)/2)
                radius: 8;color: "black";border.color: Theme.line;clip: true
                CameraItem {
                    staleInterval: root.tianji && root.status.camera_preview_mode === "snapshot" ? Math.max(10000, root.cameraIds.length*8000) : 2500
                    id: camera;objectName: "teleopCamera-"+tile.role
                    anchors.fill: parent;anchors.margins: 1;anchors.bottomMargin: root.controlsHeight+1;source: root.cameraSource(tile.modelData)
                }
                Txt {anchors.top: parent.top;anchors.right: parent.right;anchors.margins: 7;text: camera.online ? (root.tianji && root.status.camera_preview_mode === "snapshot" ? "快照" : "LIVE") : "--";font.pixelSize: 9;color: camera.online?Theme.green:"#8793a5"}
                Rectangle {
                    anchors.left: parent.left;anchors.top: parent.top;anchors.margins: 6;width: Math.min(parent.width-8,label.implicitWidth+14);height: 26
                    radius: 5;color: "#99000000"
                    Txt {id: label;objectName: "teleopCameraLabel-"+tile.role;anchors.centerIn: parent;width: Math.min(implicitWidth,parent.width-6);text: root.cameraLabel(tile.modelData);font.pixelSize: 12;color: "white";font.weight: Font.DemiBold}
                }
                Txt {objectName: "teleopCameraStale-"+tile.role;visible: camera.hasFrame && !camera.online;anchors.bottom: parent.bottom;anchors.horizontalCenter: parent.horizontalCenter;anchors.bottomMargin: root.controlsHeight+6;text: "等待刷新";font.pixelSize: 10;color: "#ffb4a9"}
                Rectangle {
                    visible: root.tianji;anchors.left: parent.left;anchors.right: parent.right;anchors.bottom: parent.bottom
                    height: root.controlsHeight;color: Theme.surface
                    Column {
                        anchors.fill: parent
                        CameraResolution {scope: "tianji";width: parent.width;height: implicitHeight;cameraKey: tile.modelData;cameraName: root.cameraLabel(tile.modelData);available: root.active && camera.online;editable: !root.locked}
                        CameraBrightness {scope: "tianji";width: parent.width;height: implicitHeight;cameraKey: tile.modelData;cameraName: root.cameraLabel(tile.modelData);active: root.active;available: root.active && camera.online;editable: !root.locked}
                    }
                }
            }
        }
        Rectangle {
            anchors.left: parent.left;anchors.right: parent.right;anchors.bottom: parent.bottom
            anchors.bottomMargin: root.controlsHeight
            height: Math.min(76, cameraFailure.implicitHeight+16)
            visible: !!root.status.camera_error && root.cameraIds.length>0
            color: Theme.redSoft;radius: 6;border.color: Theme.line
            Txt {
                id: cameraFailure;objectName: "teleopCameraError"
                anchors.fill: parent;anchors.margins: 8
                text: root.status.camera_error || "";color: Theme.red
                wrapMode: Text.Wrap;font.pixelSize: 11
            }
        }

    }
}
