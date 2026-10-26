import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Column {
    id: root
    property var s: backend.state
    property var k: s.kuavo
    property var service: (k.service||{}).service||{}
    property bool activeSession: !!k.active || ["starting","running","stopping","unknown"].indexOf(k.state)>=0
    property var savedMotion: (k.motion_settings||{}).values||({position_scale:1,max_velocity_rad_s:.7,planning_acceleration_rad_s2:5})
    property bool motionDirty: s.kuavoPositionScale!==savedMotion.position_scale || s.kuavoVelocity!==savedMotion.max_velocity_rad_s || s.kuavoAcceleration!==savedMotion.planning_acceleration_rad_s2
    property bool locked: !!s.busy || activeSession || k.state==="checking"
    property string sessionLabel: ({idle:"未运行",checking:"检查中",starting:"准备中",running:"运行中",stopping:"结束中",error:"异常",unknown:"状态未知"}[k.state]||k.state)
    spacing: 12
    Txt {visible:!!gripperDialog.error&&!gripperDialog.opened;width:parent.width;wrapMode:Text.Wrap;text:"夹爪参数自动保存失败，请打开其它参数重试："+gripperDialog.error;color:Theme.red;font.pixelSize:12}
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
                        Select {objectName:"kuavoArmMode";Layout.alignment:Qt.AlignVCenter;Layout.topMargin:app.width>=780?8:0;Layout.preferredWidth:68;model:["双臂","左臂","右臂"];currentIndex:["dual","left","right"].indexOf(s.kuavoArmMode);font.pixelSize:12;enabled:!root.locked;onActivated:backend.setValue("kuavoArmMode",["dual","left","right"][currentIndex])}
                        GridLayout {Layout.fillWidth:true;Layout.row:app.width<780?1:0;Layout.column:app.width<780?0:1;Layout.columnSpan:app.width<780?2:1;columns:app.width<480?1:3;columnSpacing:6
                            NumberField {objectName:"kuavoScale";Layout.fillWidth:true;showRange:true;spec:({label:"位移倍率",unit:"倍",min:.1,max:1,step:.1});value:s.kuavoPositionScale;enabled:!root.locked;onEdited:n=>backend.setValue("kuavoPositionScale",n)}
                            NumberField {objectName:"kuavoVelocity";Layout.fillWidth:true;showRange:true;spec:({label:"速度",unit:"rad/s",min:.01,max:.7,step:.05});value:s.kuavoVelocity;enabled:!root.locked;onEdited:n=>backend.setValue("kuavoVelocity",n)}
                            NumberField {objectName:"kuavoAcceleration";Layout.fillWidth:true;showRange:true;spec:({label:"加速度",unit:"rad/s²",min:.01,max:5,step:.1});value:s.kuavoAcceleration;enabled:!root.locked;onEdited:n=>backend.setValue("kuavoAcceleration",n)}
                        }
                        Btn {objectName:"kuavoOtherParameters";Layout.alignment:Qt.AlignVCenter;Layout.topMargin:app.width>=780?8:0;Layout.row:0;Layout.column:app.width<780?1:2;text:"其它参数";glyph:"Settings2";textSize:12;enabled:!root.locked&&!gripperDialog.saving&&!s.kuavoMotionSaving&&!s.kuavoMotionPending;onClicked:gripperDialog.open()}
                    }
                    Txt {visible:!!s.kuavoMotionError||(!root.motionDirty&&(k.motion_settings||{}).supported===false);width:parent.width;wrapMode:Text.Wrap;text:s.kuavoMotionError||"速度、加速度尚未应用，当前服务仅支持 0.7 rad/s、5 rad/s²";font.pixelSize:11;color:Theme.amber}
                }
            }
            Item {Layout.fillWidth:true;Layout.preferredWidth:375;Layout.preferredHeight:102
                Rectangle {width:setupGrid.columns===1?parent.width:1;height:setupGrid.columns===1?1:parent.height;color:Theme.line}
                    Devices {
                        objectName:"kuavoDevices";x:12;y:12;width:parent.width-24
                        checking:s.busy==="check" || k.state==="checking"
                        property bool statusKnown:["unknown","error"].indexOf(k.state)<0
                        items:[
                            {id:"robot",name:"乐聚 KUAVO 5-W",status:statusKnown && !!(k.service||{}).management_ready?"connected":"offline",detail:"最近一次检查的机器人服务连接与版本核验结果，不代表电机已使能。"},
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
                Btn {objectName:"kuavoStart";visible:!root.activeSession;text:"开始遥操作";glyph:"Play";primary:true;enabled:!root.locked && !!(k.service||{}).management_ready && service.ready_to_start===true && k.pico_sdk_available && k.vr_ready && k.pico_ready && !gripperDialog.dirty && !gripperDialog.saving && !root.motionDirty && !s.kuavoMotionPending && !s.kuavoMotionSaving && (k.motion_settings||{}).supported!==false && (!(k.gripper_settings||{}).saved || !!k.gripper_settings.matches_service);onClicked:confirmation.open()}
                Btn {objectName:"kuavoStop";visible:root.activeSession;text:k.state==="stopping"?"正在结束…":"结束遥操作";glyph:"Square";primary:true;danger:true;enabled:root.activeSession && !!k.session_id && k.state!=="stopping";onClicked:backend.teleop("stop")}
                Btn {objectName:"kuavoCheck";text:"检查设备";glyph:"RefreshCw";busy:s.busy==="check";enabled:!root.locked && !s.kuavoMotionSaving && (!s.kuavoMotionPending || !!s.kuavoMotionError);onClicked:backend.teleop("check")}
                Txt {height:34;text:"会话："+root.sessionLabel+"  ·  本地日志自动保存";font.pixelSize:12;color:Theme.inkMuted}
            }
        }
    }
    GridLayout {
        id:visualGrid;objectName:"kuavoVisualGrid";width:parent.width;columns:width<1080?1:2;columnSpacing:12;rowSpacing:12
        Card {
            objectName:"kuavoRobotCard";Layout.fillWidth:true;Layout.preferredWidth:444;Layout.maximumWidth:visualGrid.columns===2?504:Infinity;Layout.preferredHeight:340
            Heading {width:parent.width;height:51;title:"机器人姿态"}
            Txt {visible:!!k.pose_live||!!k.pose_available;anchors.right:parent.right;anchors.rightMargin:14;y:17;text:"● "+(k.pose_live?"双臂遥测在线":"反馈已过期");font.pixelSize:11;color:Theme.inkSoft}
            Rectangle {x:10;y:61;width:parent.width-20;height:parent.height-71;radius:10;border.color:Theme.line;color:Theme.stage;clip:true
                Loader {anchors.fill:parent;active:root.visible;source:"KuavoPose.qml"}

            }
        }
        Card {
            objectName:"kuavoCameraCard";Layout.fillWidth:true;Layout.preferredWidth:716;Layout.preferredHeight:340
            Heading {width:parent.width;height:51;title:"相机预览"}
            Txt {anchors.right:parent.right;anchors.rightMargin:14;y:17;text:"待接入";font.pixelSize:11;color:Theme.inkSoft}
            RowLayout {x:10;y:61;width:parent.width-20;height:parent.height-71;spacing:8
                Rectangle {Layout.fillWidth:true;Layout.preferredWidth:2;Layout.fillHeight:true;radius:8;color:"#101114"}
                ColumnLayout {Layout.fillWidth:true;Layout.preferredWidth:1;Layout.fillHeight:true;spacing:8
                    Rectangle {Layout.fillWidth:true;Layout.fillHeight:true;radius:8;color:"#101114"}
                    Rectangle {Layout.fillWidth:true;Layout.fillHeight:true;radius:8;color:"#101114"}
                }
            }
        }
    }
    Card {objectName:"kuavoManualPlaceholder";width:parent.width;height:120
        Heading {width:parent.width;height:47;title:"手动控制"}
    }
    KuavoGripperDialog {id:gripperDialog;settings:k.gripper_settings||({});fallback:(k.reference||{}).grippers||({});editable:!root.locked}
    Dialog {
        id:confirmation;objectName:"kuavoConfirm";parent:Overlay.overlay;anchors.centerIn:parent;modal:true;width:Math.min(520,app.width-32)
        title:"开始乐聚遥操作"
        standardButtons:Dialog.Ok|Dialog.Cancel
        onAccepted:backend.teleop("start")
        contentItem:ColumnLayout {spacing:12
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:"此操作由你手动发起，可能启动控制或产生动作。请确认现场已按厂家流程准备，且没有其它遥操会话。"}
        }
    }
}
