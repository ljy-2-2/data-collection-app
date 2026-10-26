import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Dialog {
    id:root;objectName:"kuavoGripperDialog"
    property var settings: ({})
    property var fallback: ({})
    property var draft: ({left:{},right:{}})
    property bool submitted:false
    property bool saving:false
    property bool dirty:false
    property bool editable:true
    property string error:""
    function edit(side,key,value) {
        if(draft[side][key]===value)return
        let next=JSON.parse(JSON.stringify(draft));next[side][key]=value;draft=next
        dirty=true;error=""
    }
    function resetDraft() {
        let values=settings.values||fallback
        let next={left:{},right:{}}
        for (let side of ["left","right"]) for(let key of ["open_rad","closed_rad","speed_rad_s","accel_rad_s2"])
            next[side][key]=(values[side]||{})[key]===undefined?0:values[side][key]
        draft=next;error="";dirty=false
    }
    parent:Overlay.overlay;anchors.centerIn:parent;modal:true;padding:20
    width:Math.min(560,parent?parent.width-32:560)
    height:Math.min(parent?parent.height-48:700,body.implicitHeight+40)
    closePolicy:Popup.CloseOnEscape|Popup.CloseOnPressOutside
    background:Rectangle {color:Theme.surface;radius:12;border.color:Theme.line}
    Overlay.modal:Rectangle {color:"#52000000"}
    onOpened:{submitted=false;if(!dirty&&!saving)resetDraft()}
    onClosed:{if(!submitted)resetDraft()}
    function confirmParameters() {
        let next=JSON.parse(JSON.stringify(draft))
        for(let i=0;i<extraFields.count;i++) {
            let field=extraFields.itemAt(i);next[field.spec.side][field.spec.key]=field.pendingValue()
        }
        draft=next;dirty=true;saving=true;submitted=true;error=""
        backend.saveKuavoSettings("grippers",draft)
        close()
    }
    Connections {target:backend
        function onKuavoSettingsSaved(ok,message) {
            if(!root.saving)return
            root.saving=false
            if(ok){root.dirty=false;root.error=""}else root.error=message
        }
    }
    contentItem:WheelScroll {
        contentWidth:width;contentHeight:body.implicitHeight;clip:true;boundsBehavior:Flickable.StopAtBounds
        ScrollBar.vertical:ScrollBar {interactive:false}
        ColumnLayout {id:body;width:parent.width;spacing:18
            RowLayout {Layout.fillWidth:true
                Txt {text:"其它参数";font.pixelSize:16;font.weight:Font.DemiBold;Layout.fillWidth:true}

            }
            GridLayout {Layout.fillWidth:true;columns:root.width<388?1:2;columnSpacing:14;rowSpacing:14
                Repeater {id:extraFields;model:[
                    {side:"left",key:"speed_rad_s",label:"左夹爪速度",unit:"rad/s",min:.01,max:15,step:.1},
                    {side:"right",key:"speed_rad_s",label:"右夹爪速度",unit:"rad/s",min:.01,max:15,step:.1},
                    {side:"left",key:"accel_rad_s2",label:"左夹爪加速度",unit:"rad/s²",min:.01,max:200,step:1},
                    {side:"right",key:"accel_rad_s2",label:"右夹爪加速度",unit:"rad/s²",min:.01,max:200,step:1},
                    {side:"left",key:"open_rad",label:"左夹爪打开角度",unit:"rad",min:-3.12,max:3.12,step:.01},
                    {side:"right",key:"open_rad",label:"右夹爪打开角度",unit:"rad",min:-3.12,max:3.12,step:.01},
                    {side:"left",key:"closed_rad",label:"左夹爪关闭角度",unit:"rad",min:-3.12,max:3.12,step:.01},
                    {side:"right",key:"closed_rad",label:"右夹爪关闭角度",unit:"rad",min:-3.12,max:3.12,step:.01}]
                    NumberField {required property var modelData;objectName:"kuavo-"+modelData.side+"-"+modelData.key;Layout.fillWidth:true;spec:modelData;value:root.draft[modelData.side][modelData.key]||0;showRange:false;arrows:false;enabled:root.editable&&!root.saving;onEdited:n=>root.edit(modelData.side,modelData.key,n)}
                }
            }
            RowLayout {Layout.fillWidth:true
                Item {Layout.fillWidth:true}
                Btn {objectName:"kuavoGripperConfirm";text:"确认";primary:true;enabled:root.editable&&!root.saving;onClicked:root.confirmParameters()}
                Btn {objectName:"kuavoGripperCancel";text:"取消";enabled:!root.saving;onClicked:root.close()}
            }
            Txt {visible:!!root.error;Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.error;color:Theme.red;font.pixelSize:12}
        }
    }
}
