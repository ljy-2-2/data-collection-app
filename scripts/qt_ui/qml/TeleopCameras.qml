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
    // Fixed role slots: losing a camera must not remove or reorder a preview.
    readonly property string cameraKeys: tianji
        ? "usb_1|usb_4|usb_2"
        : "head_color|hand_left_color|hand_right_color"
    readonly property var cameraIds: cameraKeys.split("|")
    readonly property bool splitView: width >= 700 && cameraIds.some(id => role(id) === "center") && cameraIds.length > 1
    readonly property var sideIds: cameraIds.filter(id => role(id) !== "center")
    readonly property real sideWidth: (width-28)*(tianji ? .34 : .51)
    function cameraAspect(id) {
        var selected = (((backend.teleopCameraSettings || {})[id] || {}).resolution || {}).selected || {}
        return selected.width > 0 && selected.height > 0 ? selected.width/selected.height : 4/3
    }
    function sideHeight(id) {
        // Preview geometry depends on window width, never recording resolution.
        var aspect = tianji ? 4/3 : cameraAspect(id)
        return Math.max(100, Math.min(240, (sideWidth-4)/aspect+4))
    }
    readonly property real sideStackHeight: sideIds.reduce((sum, id) => sum+sideHeight(id),0)+Math.max(0,sideIds.length-1)*8
    implicitHeight: 71 + (splitView ? sideStackHeight
        : Math.max(1,cameraIds.length)*(Math.max(180, Math.min(280,(width-20)*.75))+8)-8)
    function role(id) {
        return ({usb_4:"left",usb_1:"center",usb_2:"right",hand_left_color:"left",head_color:"center",hand_right_color:"right"})[id] || id
    }
    function cameraLabel(id) {
        var entry = ((tianji ? status : previewStatus).cameras || []).find(c => c.id === id)
        return (entry && entry.label) || ({left:"左腕部相机",center:"主相机",right:"右腕部相机"})[role(id)] || id
    }
    function cameraSource(id) {
        var entry = ((tianji ? status : previewStatus).cameras || []).find(c => c.id === id)
        return active && entry && entry.connected !== false && (tianji || entry.available)
            ? backend.baseUrl + (tianji ? "/tianji/cameras/"+id+"/stream" : "/cameras/"+id+"/frame") : ""
    }
    Heading {width: parent.width;title: "相机画面";height: 51}
    Txt {objectName:"cameraApplyStatus";visible:root.tianji && !!backend.state.cameraApplyMessage
        x:120;y:17;width:Math.max(0,root.width-260);horizontalAlignment:Text.AlignRight;font.pixelSize:11
        text:backend.state.cameraApplyMessage;color:backend.state.cameraApplyError?Theme.red:Theme.inkMuted
    }
    Btn {objectName: "teleopCameraSettings";visible: root.tianji;anchors.right: parent.right;anchors.rightMargin: 12;y: 9
        text: "相机参数";glyph: "Settings2";textSize: 12;enabled: root.active && !root.locked && !backend.state.cameraApplyBusy
        onClicked: settingsDialog.open()
    }
    TianjiCameraDialog {id: settingsDialog;cameraStatus: root.status;active: root.active}
    Item {
        id: images
        objectName: "teleopCameraImages"
        x: 10;y: 61;width: parent.width-20;height: root.height-y-10
        readonly property real mainWidth: width-8-root.sideWidth
        Repeater {
            model: root.cameraIds
            Rectangle {
                id: tile
                required property string modelData
                required property int index
                property string role: root.role(modelData)
                objectName: "teleopCameraTile-"+role
                readonly property int sideIndex: root.sideIds.indexOf(modelData)
                x: root.splitView && role !== "center" ? images.mainWidth+8 : 0
                y: root.splitView ? (role === "center" ? 0 : root.sideIds.slice(0,sideIndex).reduce((sum,id)=>sum+root.sideHeight(id)+8,0)) : index*(height+8)
                width: root.splitView ? (role === "center" ? images.mainWidth : root.sideWidth) : images.width
                height: root.splitView ? (role === "center" ? images.height : root.sideHeight(modelData)) : (images.height-8*Math.max(0,root.cameraIds.length-1))/Math.max(1,root.cameraIds.length)
                radius: 8;color: Theme.surface;border.color: Theme.line;clip: true
                Rectangle {
                    id: viewport;objectName: "teleopCameraViewport-"+tile.role
                    readonly property real availableWidth: parent.width-2
                    readonly property real availableHeight: parent.height-2
                    readonly property real aspect: camera.aspectRatio > 0 ? camera.aspectRatio : root.cameraAspect(tile.modelData)
                    width: root.tianji || tile.role==="center" ? availableWidth : Math.min(availableWidth,availableHeight*aspect)
                    height: root.tianji || tile.role==="center" ? availableHeight : width/aspect
                    x: 1+(availableWidth-width)/2;y: 1+(availableHeight-height)/2
                    color: "black";radius: 7;clip: true
                    CameraItem {
                        staleInterval: root.tianji && root.status.camera_preview_mode === "snapshot" ? Math.max(10000, root.cameraIds.length*8000) : 2500
                        id: camera;objectName: "teleopCamera-"+tile.role
                        anchors.fill: parent;source: root.cameraSource(tile.modelData)
                        visible: online
                    }
                    Txt {anchors.top: parent.top;anchors.right: parent.right;anchors.margins: 7;text: camera.online ? (root.tianji && root.status.camera_preview_mode === "snapshot" ? "快照" : "LIVE") : "--";font.pixelSize: 9;color: camera.online?Theme.green:"#8793a5"}
                    Rectangle {
                        anchors.left: parent.left;anchors.top: parent.top;anchors.margins: 6;width: Math.min(parent.width-8,label.implicitWidth+14);height: 26
                        radius: 5;color: "#99000000"
                        Txt {id: label;objectName: "teleopCameraLabel-"+tile.role;anchors.centerIn: parent;width: Math.min(implicitWidth,parent.width-6);text: root.cameraLabel(tile.modelData);font.pixelSize: 12;color: "white";font.weight: Font.DemiBold}
                    }
                }
            }
        }
    }
}
