import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Dialog {
    id:root;objectName:"kuavoSessionSettingsDialog"
    property var form: ({})
    property var draft: ({})
    property bool busy:false
    property string operation:""
    property string error:""
    property var fields: {
        let order=["arm_scale","arm_speed_rad_s","arm_acceleration_rad_s2","gripper_speed_rad_s","gripper_acceleration_rad_s2","gripper_open_target_rad","gripper_closed_target_rad"]
        return Object.keys(form.schema||{}).filter(key=>key.indexOf("gripper_")>=0).sort((a,b)=>{
            let aa=a.replace(/^(left|right)_/,""),bb=b.replace(/^(left|right)_/,"")
            return order.indexOf(aa)-order.indexOf(bb)||a.localeCompare(b)
        })
    }
    function fieldSpec(key) {
        let suffix=key.replace(/^(left|right)_/,"")
        let labels={arm_scale:"臂位移倍率",arm_speed_rad_s:"臂速度",arm_acceleration_rad_s2:"臂加速度",gripper_speed_rad_s:"夹爪速度",gripper_acceleration_rad_s2:"夹爪加速度",gripper_open_target_rad:"夹爪打开角度",gripper_closed_target_rad:"夹爪关闭角度"}
        let spec=Object.assign({},form.schema[key])
        spec.label=(key.indexOf("left_")===0?"左":"右")+(labels[suffix]||suffix)
        spec.unit=spec.unit==="x"?"倍":spec.unit==="rad/s^2"?"rad/s²":spec.unit
        return spec
    }
    parent:Overlay.overlay;anchors.centerIn:parent;modal:true;padding:20
    width:Math.min(560,parent?parent.width-32:560)
    height:Math.min(parent?parent.height-48:700,body.implicitHeight+40)
    closePolicy:busy?Popup.NoAutoClose:Popup.CloseOnEscape|Popup.CloseOnPressOutside
    background:Rectangle {color:Theme.surface;radius:12;border.color:Theme.line}
    Overlay.modal:Rectangle {color:"#52000000"}
    function send(action) {
        if(busy)return
        let payload={action:action}
        try {
            if(action==="apply") {
                let changes={}
                for(let i=0;i<extraFields.count;i++) {
                    let field=extraFields.itemAt(i),key=field.modelData,n=field.pendingValue(),spec=form.schema[key]
                    if(!Number.isFinite(n)||n<spec.min||n>spec.max||(spec.min_exclusive&&n===spec.min))throw new Error(spec.label+"超出范围")
                    if(n!==form.values[key])changes[key]=n
                }
                if(!Object.keys(changes).length){close();return}
                payload.changes=changes
                payload.expected_revision=form.revision
                payload.expected_service_instance_id=form.service_instance_id
                payload.expected_installation_sha256=form.installation_sha256
                payload.confirm=true
            }
        } catch(e) {error=String(e);return}
        operation=action;busy=true;error=""
        backend.kuavoSessionSettings(payload)
    }
    onOpened:{form={};draft={};send("read")}
    Connections {target:backend
        function onKuavoSessionSettingsDone(ok,message) {
            if(!root.busy)return
            root.busy=false
            if(!ok){root.error=message;return}
            root.form=backend.state.kuavo.session_settings||{}
            root.draft=JSON.parse(JSON.stringify(root.form.values||{}))
            if(root.operation==="apply")root.close()
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
                Repeater {id:extraFields;model:root.fields
                    NumberField {required property string modelData;objectName:"kuavo-service-"+modelData;Layout.fillWidth:true
                        spec:root.fieldSpec(modelData);value:root.draft[modelData]||0;showRange:false;arrows:false
                        enabled:!root.busy&&root.form.editable===true
                        onEdited:n=>{let next=Object.assign({},root.draft);next[modelData]=n;root.draft=next;root.error=""}
                    }
                }
            }
            RowLayout {Layout.fillWidth:true
                Item {Layout.fillWidth:true}
                Btn {objectName:"kuavoServiceApply";text:"确认";primary:true;enabled:!root.busy&&root.form.editable===true;onClicked:root.send("apply")}
                Btn {objectName:"kuavoServiceCancel";text:"取消";enabled:!root.busy;onClicked:root.close()}
            }
            Txt {visible:!!root.error||!!root.form.locked_reason;Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.error||root.form.locked_reason||"";color:Theme.red;font.pixelSize:12}
        }
    }
}
