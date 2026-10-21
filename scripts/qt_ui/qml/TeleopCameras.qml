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
    readonly property bool splitView: width >= 700 && cameraIds.some(id => role(id) === "center") && cameraIds.length > 1
    readonly property var sideIds: cameraIds.filter(id => role(id) !== "center")
    readonly property real sideWidth: (width-28)*.51
    function cameraAspect(id) {
        var selected = (((backend.teleopCameraSettings || {})[id] || {}).resolution || {}).selected || {}
        return selected.width > 0 && selected.height > 0 ? selected.width/selected.height : 4/3
    }
    function sideHeight(id) {
        return Math.max(tianji ? 136 : 100, Math.min(240, (sideWidth-(tianji ? 184 : 0)-4)/cameraAspect(id)+4))
    }
    readonly property real sideStackHeight: sideIds.reduce((sum, id) => sum+sideHeight(id),0)+Math.max(0,sideIds.length-1)*8
    implicitHeight: 71 + (splitView ? sideStackHeight
        : Math.max(1,cameraIds.length)*(Math.max(180, Math.min(280,(width-20-(tianji ? 184 : 0))*.75))+8)-8)
    function role(id) {
        return ({usb_4:"left",usb_1:"center",usb_2:"right",hand_left_color:"left",head_color:"center",hand_right_color:"right"})[id] || id
    }
    function displayOrder(id) {
        var order = ["center", "left", "right"].indexOf(role(id))
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
    Item {
        id: images
        objectName: "teleopCameraImages"
        x: 10;y: 61;width: parent.width-20;height: root.height-y-10
        readonly property real mainWidth: width-8-root.sideWidth
        Txt {anchors.centerIn: parent;visible: root.cameraIds.length === 0;text: root.status.camera_error || "";width: parent.width-20;wrapMode: Text.Wrap;font.pixelSize: 12;horizontalAlignment: Text.AlignHCenter}
        Repeater {
            model: root.cameraIds
            Rectangle {
                id: tile
                required property string modelData
                required property int index
                property string role: root.role(modelData)
                objectName: "teleopCameraTile-"+role
                readonly property bool bottomParameters: role === "center"
                readonly property real panelWidth: root.tianji && !bottomParameters ? 184 : 0
                readonly property real panelHeight: root.tianji && bottomParameters ? (width >= 400 ? 68 : 92) : 0
                readonly property int sideIndex: root.sideIds.indexOf(modelData)
                x: root.splitView && role !== "center" ? images.mainWidth+8 : 0
                y: root.splitView ? (role === "center" ? 0 : root.sideIds.slice(0,sideIndex).reduce((sum,id)=>sum+root.sideHeight(id)+8,0)) : index*(height+8)
                width: root.splitView ? (role === "center" ? images.mainWidth : root.sideWidth) : images.width
                height: root.splitView ? (role === "center" ? images.height : root.sideHeight(modelData)) : (images.height-8*Math.max(0,root.cameraIds.length-1))/Math.max(1,root.cameraIds.length)
                radius: 8;color: Theme.surface;border.color: Theme.line;clip: true
                Rectangle {
                    id: viewport;objectName: "teleopCameraViewport-"+tile.role
                    readonly property real availableWidth: parent.width-tile.panelWidth-2
                    readonly property real availableHeight: parent.height-tile.panelHeight-2
                    readonly property real aspect: camera.aspectRatio > 0 ? camera.aspectRatio : root.cameraAspect(tile.modelData)
                    width: tile.bottomParameters ? availableWidth : Math.min(availableWidth,availableHeight*aspect)
                    height: tile.bottomParameters ? availableHeight : width/aspect
                    x: 1+(availableWidth-width)/2;y: 1+(availableHeight-height)/2
                    color: "black";radius: 7;clip: true
                    CameraItem {
                        staleInterval: root.tianji && root.status.camera_preview_mode === "snapshot" ? Math.max(10000, root.cameraIds.length*8000) : 2500
                        id: camera;objectName: "teleopCamera-"+tile.role
                        anchors.fill: parent;source: root.cameraSource(tile.modelData)
                    }
                    Txt {anchors.top: parent.top;anchors.right: parent.right;anchors.margins: 7;text: camera.online ? (root.tianji && root.status.camera_preview_mode === "snapshot" ? "快照" : "LIVE") : "--";font.pixelSize: 9;color: camera.online?Theme.green:"#8793a5"}
                    Rectangle {
                        anchors.left: parent.left;anchors.top: parent.top;anchors.margins: 6;width: Math.min(parent.width-8,label.implicitWidth+14);height: 26
                        radius: 5;color: "#99000000"
                        Txt {id: label;objectName: "teleopCameraLabel-"+tile.role;anchors.centerIn: parent;width: Math.min(implicitWidth,parent.width-6);text: root.cameraLabel(tile.modelData);font.pixelSize: 12;color: "white";font.weight: Font.DemiBold}
                    }
                }
                Rectangle {
                    objectName: "teleopCameraParameters-"+tile.role
                    visible: root.tianji
                    x: tile.bottomParameters ? 1 : tile.width-tile.panelWidth
                    y: tile.bottomParameters ? tile.height-tile.panelHeight : 0
                    width: tile.bottomParameters ? tile.width-2 : tile.panelWidth
                    height: tile.bottomParameters ? tile.panelHeight : tile.height;color: Theme.surface
                    Rectangle {width: tile.bottomParameters ? parent.width : 1;height: tile.bottomParameters ? 1 : parent.height;color: Theme.line}
                    GridLayout {
                        id: parameterGrid
                        anchors.left: parent.left;anchors.right: parent.right;anchors.verticalCenter: parent.verticalCenter
                        columns: tile.bottomParameters && tile.width >= 400 ? 2 : 1;columnSpacing: 0;rowSpacing: 0
                        CameraResolution {scope: "tianji";sidePanel: !tile.bottomParameters || parameterGrid.columns===2;Layout.fillWidth: true;Layout.preferredWidth: 200;Layout.preferredHeight: implicitHeight;cameraKey: tile.modelData;cameraName: root.cameraLabel(tile.modelData);available: root.active && camera.online;editable: !root.locked}
                        CameraBrightness {scope: "tianji";sidePanel: !tile.bottomParameters || parameterGrid.columns===2;Layout.fillWidth: true;Layout.preferredWidth: 200;Layout.preferredHeight: implicitHeight;cameraKey: tile.modelData;cameraName: root.cameraLabel(tile.modelData);active: root.active;available: root.active && camera.online;editable: !root.locked}
                    }
                }
            }
        }
        Rectangle {
            anchors.left: parent.left;anchors.right: parent.right;anchors.bottom: parent.bottom
            anchors.bottomMargin: 4
            height: Math.min(76, cameraFailure.implicitHeight+16)
            visible: (!!root.status.camera_error || !!root.status.camera_joint_error) && root.cameraIds.length>0
            color: Theme.redSoft;radius: 6;border.color: Theme.line
            Txt {
                id: cameraFailure;objectName: "teleopCameraError"
                anchors.fill: parent;anchors.margins: 8
                text: root.status.camera_error || (root.status.camera_joint_error ? "关节反馈中断，暂不能采集；视频继续显示。\n"+root.status.camera_joint_error : "");color: Theme.red
                wrapMode: Text.Wrap;font.pixelSize: 11
            }
        }

    }
}
