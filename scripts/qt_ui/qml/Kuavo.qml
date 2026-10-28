import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Column {
    id: root
    property var s: backend.state
    property var k: s.kuavo
    property var collection: k.collection||({})
    property string collectionLabel: collection.recording ? "● 采集中" : collection.busy ? "正在保存" : collection.state==="incomplete" ? "本轮不完整" : collection.state==="disabled" ? "采集已关闭" : collection.armed ? "等待手柄 "+((collection.settings||{}).start_button||"A") : "未开始遥操作"
    property bool serviceParameters: k.parameter_mode==="service"
    property var service: (k.service||{}).service||{}
    property bool activeSession: (!!k.active && ["checking","preparing","ready"].indexOf(k.state)<0) || ["starting","running","stopping","unknown"].indexOf(k.state)>=0
    property var controls: (k.motion_controls||{})[s.kuavoArmMode]||{}
    property bool prestartEditable: k.prestart_editable===undefined?["idle","error","checking","preparing","ready"].indexOf(k.state)>=0:!!k.prestart_editable
    property bool canReturn: prestartEditable && s.busy!=="return-config"
    property bool editLocked: !prestartEditable || (s.busy!=="" && s.busy!=="check")
    property bool motionEditable: !editLocked && !s.kuavoMotionSaving && (!serviceParameters || (!!controls.values && !!(k.session_settings||{}).editable))
    function fieldSpec(key,label,unit,min,max,step) {
        let spec=(controls.schema||{})[key]
        return {label:label,unit:unit,min:serviceParameters&&spec?(spec.min_exclusive?spec.min+0.000001:spec.min):min,max:serviceParameters&&spec?spec.max:max,step:step}
    }
    property var savedMotion: (k.motion_settings||{}).values||({position_scale:1,max_velocity_rad_s:.7,planning_acceleration_rad_s2:5})
    property bool motionDirty: !serviceParameters && (s.kuavoPositionScale!==savedMotion.position_scale || s.kuavoVelocity!==savedMotion.max_velocity_rad_s || s.kuavoAcceleration!==savedMotion.planning_acceleration_rad_s2)
    property bool locked: !!s.busy || activeSession || ["checking","preparing","ready"].indexOf(k.state)>=0
    spacing: 12
    Card {
        width: parent.width;height: setupGrid.height+footer.height
        GridLayout {
            id: setupGrid;width: parent.width;columns: app.width<=1180 ? 1 : 2;columnSpacing: 0;rowSpacing: 0
            Item {
                Layout.fillWidth: true;Layout.preferredWidth: 620;Layout.preferredHeight: modeColumn.height+24
                Column {id:modeColumn;x:12;y:12;width:parent.width-24;spacing:12
                    RowLayout {width:parent.width;height:20;spacing:5
                        Txt {text:"01";font.pixelSize:11;color:Theme.accentStrong;Layout.preferredWidth:22}
                        Txt {text:"遥操作设置";font.weight:Font.DemiBold}
                        Item {Layout.fillWidth:true}
                    }
                    GridLayout {width:parent.width;columns:app.width<780?2:3;columnSpacing:8;rowSpacing:8
                        Select {objectName:"kuavoArmMode";Layout.alignment:Qt.AlignVCenter;Layout.topMargin:app.width>=780?8:0;Layout.preferredWidth:108;model:["双臂","双臂＋底盘","左臂","右臂"];currentIndex:["dual","dual_base","left","right"].indexOf(s.kuavoArmMode);font.pixelSize:12;enabled:!root.editLocked&&!s.kuavoMotionPending&&!s.kuavoMotionSaving;onActivated:backend.setValue("kuavoArmMode",["dual","dual_base","left","right"][currentIndex])}
                        GridLayout {Layout.fillWidth:true;Layout.row:app.width<780?1:0;Layout.column:app.width<780?0:1;Layout.columnSpan:app.width<780?2:1;columns:app.width<480?1:3;columnSpacing:6
                            NumberField {objectName:"kuavoScale";Layout.fillWidth:true;showRange:!root.serviceParameters||!!root.controls.schema;spec:root.fieldSpec("position_scale","位移倍率","倍",.1,1,.1);value:s.kuavoPositionScale;enabled:root.motionEditable;onEdited:n=>backend.setValue("kuavoPositionScale",n)}
                            NumberField {objectName:"kuavoVelocity";Layout.fillWidth:true;showRange:!root.serviceParameters||!!root.controls.schema;spec:root.fieldSpec("max_velocity_rad_s","速度","rad/s",.01,.7,.05);value:s.kuavoVelocity;enabled:root.motionEditable;onEdited:n=>backend.setValue("kuavoVelocity",n)}
                            NumberField {objectName:"kuavoAcceleration";Layout.fillWidth:true;showRange:!root.serviceParameters||!!root.controls.schema;spec:root.fieldSpec("planning_acceleration_rad_s2","加速度","rad/s²",.01,5,.1);value:s.kuavoAcceleration;enabled:root.motionEditable;onEdited:n=>backend.setValue("kuavoAcceleration",n)}
                        }
                        Btn {objectName:"kuavoOtherParameters";Layout.alignment:Qt.AlignVCenter;Layout.topMargin:app.width>=780?8:0;Layout.row:0;Layout.column:app.width<780?1:2;text:"其它参数";glyph:"Settings2";textSize:12;enabled:!root.editLocked&&!gripperDialog.saving&&!s.kuavoMotionSaving&&!s.kuavoMotionPending;onClicked:root.serviceParameters?sessionSettingsDialog.open():gripperDialog.open()}
                    }
                }
            }
            Item {Layout.fillWidth:true;Layout.preferredWidth:375;Layout.preferredHeight:102
                Rectangle {width:setupGrid.columns===1?parent.width:1;height:setupGrid.columns===1?1:parent.height;color:Theme.line}
                    Devices {
                        objectName:"kuavoDevices";x:12;y:12;width:parent.width-24
                        checking:s.busy==="check" || k.state==="checking"
                        property bool statusKnown:["unknown","error"].indexOf(k.state)<0
                        items:[
                            {id:"robot",name:"乐聚 KUAVO 5-W",status:!!(k.service||{}).management_ready?"connected":"offline",detail:k.service_block_detail||k.hardware_detail||"最近一次检查的机器人服务连接与版本核验结果，不代表电机已使能。"},
                            {id:"xr",name:"Galaxis VR",status:(root.activeSession?!!k.pico_live:!!k.vr_ready)?"connected":"offline",detail:root.activeSession?(k.pico_live?"正在接收实时头显输入":"等待新的头显输入"):(k.vr_detail||"尚未检查头显输入")},
                            {id:"headset",name:"Pico 设备",status:(root.activeSession?!!k.pico_live:!!k.pico_ready)?"connected":"offline",detail:k.pico_detail||"尚未检查 PICO USB 和头显应用"}
                        ]
                    }
            }
        }
        Rectangle {
            id:footer;y:setupGrid.height;width:parent.width;height:footerFlow.implicitHeight+12;color:Theme.muted
            Rectangle {width:parent.width;height:1;color:Theme.line}
            Flow {id:footerFlow;x:12;y:6;width:parent.width-24;spacing:6;layoutDirection:Qt.RightToLeft
                Btn {objectName:"kuavoStart";visible:!root.activeSession;text:"开始遥操作";glyph:"Play";primary:true;enabled:(!!k.control_ready || !!k.can_restart) && !s.busy && !!(k.service||{}).management_ready && service.ready_to_start===true && k.pico_sdk_available && k.vr_ready && k.pico_ready && !k.start_blocked_reason && !s.kuavoMotionPending && !s.kuavoMotionSaving && (s.kuavoArmMode!=="dual_base" || !!k.base_supported) && (root.serviceParameters ? !!k.session_settings : (!gripperDialog.dirty && !gripperDialog.saving && !root.motionDirty && (k.motion_settings||{}).supported!==false && (!(k.gripper_settings||{}).saved || !!k.gripper_settings.matches_service)));onClicked:backend.teleop("start")}
                Btn {objectName:"kuavoStop";visible:root.activeSession&&!root.prestartEditable&&!((k.manual||{}).active);text:k.state==="stopping"?"正在结束…":"结束遥操作";glyph:"Square";primary:true;danger:true;enabled:(root.prestartEditable||!!k.session_id) && k.state!=="stopping" && s.busy!=="return-config" && s.busy!=="stop";onClicked:backend.teleop("stop")}
                Btn {objectName:"kuavoCheck";text:"检查设备";glyph:"RefreshCw";busy:s.busy==="check";enabled:!root.locked && !s.kuavoMotionSaving && (!s.kuavoMotionPending || !!s.kuavoMotionError);onClicked:backend.teleop("check")}
                Row {spacing:14;height:34;layoutDirection:Qt.LeftToRight
                    Txt {objectName:"kuavoCompletedEpisodes";height:34;text:"本次已完成  "+(root.collection.completed||0)+" Episode";font.pixelSize:12;color:Theme.inkMuted}
                    Txt {objectName:"kuavoCollectionStatus";height:34;text:root.collectionLabel;color:root.collection.recording?Theme.red:Theme.inkMuted;font.pixelSize:12}
                }
            }
        }
    }
    GridLayout {
        id:visualGrid;objectName:"kuavoVisualGrid";width:parent.width;columns:width<1080?1:2;columnSpacing:12;rowSpacing:12
        Card {
            objectName:"kuavoRobotCard";Layout.fillWidth:true;Layout.preferredWidth:444;Layout.maximumWidth:visualGrid.columns===2?504:Infinity;Layout.preferredHeight:340
            Heading {width:parent.width;height:51;title:"机器人姿态"}
            Rectangle {x:10;y:61;width:parent.width-20;height:parent.height-71;radius:10;border.color:Theme.line;color:Theme.stage;clip:true
                Loader {anchors.fill:parent;active:root.visible;source:"KuavoPose.qml"}

            }
        }
        KuavoCameras {
            objectName:"kuavoCameraCard";Layout.fillWidth:true;Layout.preferredWidth:716;Layout.preferredHeight:340
            status:k.camera_preview||({});active:root.visible
        }
    }
    KuavoManual {width:parent.width}
    KuavoSessionSettingsDialog {id:sessionSettingsDialog}
    KuavoGripperDialog {id:gripperDialog;settings:k.gripper_settings||({});fallback:(k.reference||{}).grippers||({});editable:!root.editLocked}
}
