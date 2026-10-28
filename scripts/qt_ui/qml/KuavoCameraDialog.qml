import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Dialog {
    id:root
    objectName:"kuavoCameraDialog"
    property var cameraStatus: ({})
    property bool busy:false
    property bool editable:true
    property bool applying:false
    property bool retainDrafts:false
    property string error:""
    property var cameraIds:["head","left","right"]
    function camera(role) {return (cameraStatus.cameras||[]).find(c=>c.id===role)||({})}
    function details(role) {
        let config=cameraStatus.config||{}
        let source=(cameraStatus.sources||[]).find(s=>s.id===(config.roles||{})[role])||{}
        let caps=source.capabilities||{},entry=camera(role)
        let profile=(config.profiles||{})[role]||{}
        let width=entry.width||profile.width||640,height=entry.height||profile.height||480
        let sizes=(caps.resolutions||[]).map(s=>({id:s.width+"x"+s.height,label:s.width+" × "+s.height}))
        return {busy:root.busy,error:entry.error||"",
            resolution:{options:sizes,selected:{id:width+"x"+height},editable:sizes.length>0},
            brightness:caps.brightness?Object.assign({supported:true,loaded:true},caps.brightness):{supported:false,loaded:true}}
    }
    function submit() {
        let profiles={}
        for (let i=0;i<sections.count;i++) {
            let section=sections.itemAt(i)
            if (!section.valid) return
            let change=section.changes()
            if (!Object.keys(change).length) continue
            let size=(change.mode_id||section.resolution.selected.id).split("x").map(Number)
            profiles[section.cameraKey]={width:size[0],height:size[1],brightness:change.brightness!==undefined?change.brightness:section.brightness.supported?section.brightness.value:null}
        }
        if (!Object.keys(profiles).length) {close();return}
        error="";applying=true
        close()
        backend.kuavoCameraAction("settings",profiles)
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
            for (let i=0;i<sections.count;i++) sections.itemAt(i).reset()
        }
        if (!busy && cameraStatus.config && cameraIds.some(role=>camera(role).connected)) backend.kuavoCameraAction("discover",cameraStatus.config)
    }
    onAboutToHide:if (!applying) retainDrafts=false
    Connections {
        target:backend
        function onKuavoCameraSettingsApplied(ok,message) {
            if (!root.applying) return
            root.applying=false;root.retainDrafts=!ok;root.error=ok?"":message
        }
    }
    contentItem:WheelScroll {
        contentWidth:width;contentHeight:body.implicitHeight;clip:true;boundsBehavior:Flickable.StopAtBounds
        ScrollBar.vertical:ScrollBar {interactive:false}
        ColumnLayout {id:body;width:parent.width;spacing:12
            Txt {text:"相机参数";font.pixelSize:16;font.weight:Font.DemiBold}
            Repeater {id:sections;model:root.cameraIds
                TianjiCameraDraft {required property string modelData;required property int index
                    objectName:"kuavoCameraDraft-"+modelData;Layout.fillWidth:true
                    cameraKey:modelData;cameraName:["主相机","左相机","右相机"][index]
                    details:root.details(modelData)
                    available:!!root.camera(modelData).connected;editable:root.editable&&!root.applying
                }
            }
            Txt {visible:!!root.error||!!backend.state.kuavoCameraError;Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.error||backend.state.kuavoCameraError||"";font.pixelSize:12;color:Theme.red}
            RowLayout {Layout.alignment:Qt.AlignRight
                Btn {objectName:"kuavoCameraCancel";text:"取消";enabled:!root.applying;onClicked:root.close()}
                Btn {objectName:"kuavoCameraConfirm";text:root.applying?"正在应用…":"确认";primary:true
                    enabled:root.editable&&!root.busy&&!root.applying&&[0,1,2].every(i=>!!sections.itemAt(i)&&sections.itemAt(i).valid)
                    onClicked:root.submit()
                }
            }
        }
    }
}
