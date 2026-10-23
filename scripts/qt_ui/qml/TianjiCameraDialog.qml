import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Dialog {
    id: root
    objectName: "tianjiCameraDialog"
    property var cameraStatus: ({})
    property bool active: false
    property string error: ""
    property bool applying: false
    property bool retainDrafts: false
    property var cameraIds: ["usb_1","usb_4","usb_2"]
    readonly property bool editable: active && !backend.state.busy && !backend.state.closing && ["idle","error"].indexOf(cameraStatus.state)>=0 && !(cameraStatus.collection||{}).armed && !(cameraStatus.collection||{}).busy && !(cameraStatus.collection||{}).recording
    readonly property bool reading: cameraIds.some(key=>(backend.teleopCameraSettings[key]||{}).busy)
    function connected(key) {return (cameraStatus.cameras||[]).some(c=>c.id===key && c.connected!==false)}
    function submit() {
        var changes={}
        for (var i=0;i<sections.count;i++) {
            var section=sections.itemAt(i)
            if (!section.valid) return
            var change=section.changes()
            if (Object.keys(change).length) changes[section.cameraKey]=change
        }
        if (!Object.keys(changes).length) {close();return}
        error="";applying=true
        close()
        backend.applyTeleopCameraSettings(changes)
    }
    parent:Overlay.overlay;anchors.centerIn:parent
    width:Math.min(560,parent?parent.width-32:560)
    height:Math.min(parent?parent.height-48:700,body.implicitHeight+40)
    modal:true;padding:20
    closePolicy:applying?Popup.NoAutoClose:Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background:Rectangle {color:Theme.surface;radius:12;border.color:Theme.line}
    Overlay.modal:Rectangle {color:"#52000000"}
    onOpened:{
        if (!retainDrafts) {
            error=""
            for (var i=0;i<sections.count;i++) sections.itemAt(i).reset()
        }
        for (var j=0;j<cameraIds.length;j++) if (connected(cameraIds[j])) backend.loadTeleopCameraSettings(cameraIds[j])
    }
    onAboutToHide: if (!applying) retainDrafts=false
    Connections {
        target:backend
        function onTeleopCameraSettingsApplied(ok,message) {
            if (!root.applying) return
            root.applying=false
            root.retainDrafts=!ok
            root.error=ok?"":message
        }
    }
    contentItem:WheelScroll {
        contentWidth:width;contentHeight:body.implicitHeight;clip:true;boundsBehavior:Flickable.StopAtBounds
        ScrollBar.vertical:ScrollBar {interactive:false}
        ColumnLayout {
            id:body;width:parent.width;spacing:12
            Txt {text:"相机参数";font.pixelSize:16;font.weight:Font.DemiBold}
            Repeater {
                id:sections;model:root.cameraIds
                TianjiCameraDraft {required property string modelData;required property int index
                    objectName:"cameraDraft-"+modelData;Layout.fillWidth:true
                    cameraKey:modelData;cameraName:["主相机","左相机","右相机"][index]
                    available:root.connected(modelData);editable:root.editable && !root.applying
                }
            }
            Txt {visible:!!root.error;Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.error;font.pixelSize:12;color:Theme.red}
            RowLayout {Layout.alignment:Qt.AlignRight
                Btn {objectName:"cameraSettingsCancel";text:"取消";enabled:!root.applying;onClicked:root.close()}
                Btn {objectName:"cameraSettingsConfirm";text:root.applying?"正在应用…":"确认";primary:true
                    enabled:root.editable && !root.reading && !root.applying && [0,1,2].every(i=>!!sections.itemAt(i) && sections.itemAt(i).valid)
                    onClicked:root.submit()
                }
            }
        }
    }
}
