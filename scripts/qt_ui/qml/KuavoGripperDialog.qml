import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Popup {
    id:root;objectName:"kuavoGripperDialog"
    property var settings: ({})
    property var fallback: ({})
    property var draft: ({left:{},right:{}})
    property bool saving:false
    property bool editable:true
    property string error:""
    function edit(side,key,value) {let next=JSON.parse(JSON.stringify(draft));next[side][key]=value;draft=next}
    function reset(original=false) {
        let values=original?fallback:(settings.values||fallback)
        let next={left:{},right:{}}
        for (let side of ["left","right"]) for(let key of ["open_rad","closed_rad","speed_rad_s","accel_rad_s2"])
            next[side][key]=(values[side]||{})[key]===undefined?0:values[side][key]
        draft=next;error=""
    }
    parent:Overlay.overlay;anchors.centerIn:parent;modal:true;padding:20
    width:Math.min(580,parent?parent.width-32:580)
    height:Math.min(parent?parent.height-48:700,body.implicitHeight+40)
    closePolicy:saving?Popup.NoAutoClose:Popup.CloseOnEscape
    background:Rectangle {color:Theme.surface;radius:12;border.color:Theme.line}
    Overlay.modal:Rectangle {color:"#52000000"}
    onOpened:reset()
    Connections {target:backend
        function onKuavoSettingsSaved(ok,message) {if(!root.saving)return;root.saving=false;if(ok)root.close();else root.error=message}
    }
    contentItem:WheelScroll {
        contentWidth:width;contentHeight:body.implicitHeight;clip:true
        ScrollBar.vertical:ScrollBar {interactive:false}
        ColumnLayout {id:body;width:parent.width;spacing:14
            Txt {text:"其他参数 · 乐聚达妙夹爪";font.pixelSize:16;font.weight:Font.DemiBold}
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:"修改保存到本机。机器人服务更新配置后才生效，当前不支持在线应用。";color:Theme.inkMuted;font.pixelSize:12}
            Repeater {model:["left","right"]
                ColumnLayout {id:section;required property string modelData;Layout.fillWidth:true;spacing:8
                    Txt {text:section.modelData==="left"?"左夹爪":"右夹爪";font.weight:Font.DemiBold}
                    GridLayout {Layout.fillWidth:true;columns:2;columnSpacing:12;rowSpacing:8
                        Repeater {model:[{key:"open_rad",label:"张开位置",unit:"rad",min:-3.12,max:3.12,step:.01},{key:"closed_rad",label:"闭合位置",unit:"rad",min:-3.12,max:3.12,step:.01},{key:"speed_rad_s",label:"速度",unit:"rad/s",min:.01,max:15,step:.1},{key:"accel_rad_s2",label:"加速度",unit:"rad/s²",min:.01,max:200,step:1}]
                            NumberField {required property var modelData;objectName:"kuavo-"+section.modelData+"-"+modelData.key;Layout.fillWidth:true;spec:modelData;value:root.draft[section.modelData][modelData.key]||0;enabled:root.editable&&!root.saving;onEdited:n=>root.edit(section.modelData,modelData.key,n)}
                        }
                    }
                }
            }
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.error||root.settings.message||"";color:root.error?Theme.red:Theme.inkMuted;font.pixelSize:12}
            Txt {visible:!!root.settings.file;Layout.fillWidth:true;wrapMode:Text.WrapAnywhere;text:root.settings.file||"";color:Theme.inkMuted;font.pixelSize:11}
            RowLayout {Layout.alignment:Qt.AlignRight
                Btn {text:"恢复原项目值";enabled:!root.saving;onClicked:root.reset(true)}
                Btn {objectName:"kuavoGripperCancel";text:"取消";enabled:!root.saving;onClicked:root.close()}
                Btn {objectName:"kuavoGripperSave";text:root.saving?"保存中…":"确认";primary:true;enabled:root.editable&&!root.saving;onClicked:{forceActiveFocus();root.saving=true;root.error="";backend.saveKuavoSettings("grippers",root.draft)}}
            }
        }
    }
}
