import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Card {
    id: root
    property bool tianji: true
    property bool canMove: false
    property bool canRecover: false
    property bool live: false
    property bool locked: false
    property var s: backend.state
    width: parent.width
    height: body.height+heading.height+24
    Heading {id: heading;width: parent.width;height: 47;title: "手动控制"}
    Rectangle {anchors.right: parent.right;anchors.rightMargin: 14;y: 12;width: 62;height: 22;radius: 11;color: Theme.amberSoft
        Txt {anchors.centerIn: parent;text: "真实运动";font.pixelSize: 11;color: Theme.amber;font.weight: Font.DemiBold}
    }
    Column {id: body;x: 12;y: heading.height+12;width: parent.width-24;spacing: 10
        Rectangle {width: parent.width;height: toolbar.height+14;radius: 10;color: Theme.muted;border.color: Theme.line
            Flow {id: toolbar;x: 9;y: 7;width: parent.width-18;spacing: 5
                Txt {text: "姿态配置";height: 34;width: Math.max(65,parent.width-445);font.weight: Font.DemiBold}
                Select {id: poses;objectName: "pose-selector";width: 210;model: {let p=s.poses.map(x=>({id:x.id,label:x.name}));if(root.tianji)p.unshift({id:"zero",label:"零点（默认）"});if(!p.length)p=[{id:"",label:"暂无配置"}];return p}
                    textRole: "label";valueRole: "id";currentIndex: Math.max(0,model.findIndex(p=>p.id===s.selectedPose));enabled: !root.locked || root.canRecover;onActivated: backend.setValue("selectedPose",model[currentIndex].id)}
                Btn {
                    objectName: "restore-selected-pose"
                    property bool stopping: root.tianji && s.busy==="stop"
                    property bool stopMode: root.tianji && !stopping && (s.tianji.state==="moving" || s.busy==="move")
                    primary: true;danger: stopMode;text: stopMode ? "停止" : "复位"
                    width: Math.ceil(resetLabel.width)+22;height: 34
                    TextMetrics {id: resetLabel;text: "复位";font.family: Theme.font;font.pixelSize: 13;font.weight: Font.Medium}
                    enabled: stopping ? false : stopMode ? s.authenticated && !s.closing : root.tianji && s.selectedPose==="zero" ? root.canRecover : root.canMove && !!s.selectedPose
                    onClicked: {if(stopMode)backend.teleop("stop");else {confirm.action="pose";confirm.open()}}
                }
                Btn {objectName: "save-current-pose";text: "保存当前姿态";enabled: !root.locked && (!root.tianji || root.live);onClicked: {poseName.text="";nameDialog.open()}}
                Btn {objectName: "delete-selected-pose";text: "删除";ghost: true;danger: true;enabled: !root.locked && s.selectedPose!=="zero" && !!s.selectedPose;onClicked: {confirm.action="delete";confirm.open()}}
            }
        }
        GridLayout {width: parent.width;columns: app.width<620 ? 1 : root.tianji ? 2 : 3;columnSpacing: 9;rowSpacing: 9
            Repeater {
                model: root.tianji ? ["left","right"] : ["left","body","right"]
                Rectangle {
                    id: arm
                    required property string modelData
                    property string side: modelData
                    property string sideTitle: side==="left" ? "左臂" : side==="right" ? "右臂" : "身体"
                    property var specs: root.tianji ? backend.limits.map((lim,i)=>({name:side+String(i),label:sideTitle+" J"+(i+1),minimum:lim[0],maximum:lim[1]})) : backend.g2Joints.filter(j=>side==="body"?j.name.includes("body"):side==="left"?j.name.includes("_l_"):j.name.includes("_r_"))
                    Layout.fillWidth: true;Layout.alignment: Qt.AlignTop;height: joints.height+45;color: Theme.muted;border.color: Theme.line;radius: 10
                    Txt {x: 10;y: 8;height: 20;text: arm.sideTitle;font.weight: Font.DemiBold}
                    Rectangle {y: 36;width: parent.width;height: 1;color: Theme.line}
                    Column {id: joints;x: 6;y: 43;width: parent.width-12;spacing: 5
                        Repeater {
                            model: arm.specs.length
                            Rectangle {
                                id: jointRow
                                required property int index
                                property var spec: arm.specs[index]
                                property var raw: root.tianji ? (((s.tianji.joints||{})[arm.side]||[])[index]) : s.preview.joint_positions[spec.name]===undefined ? spec.default : s.preview.joint_positions[spec.name]
                                width: parent.width;height: 54;radius: 10;color: Theme.surface;border.color: Theme.line
                                Column {x: 9;y: 7;width: parent.width-124;spacing: 2
                                    Txt {width: parent.width;text: spec.label;font.weight: Font.DemiBold;height: 18}
                                    Txt {width: parent.width;text: root.tianji ? spec.minimum+"° ～ "+spec.maximum+"°" : spec.name;font.pixelSize: root.tianji ? 12 : 9;color: Theme.inkSoft;height: 17}
                                }
                                Rectangle {anchors.right: parent.right;anchors.rightMargin: 34;y: 7;width: 72;height: 40;radius: 7;color: Theme.muted;border.color: Theme.line
                                    Txt {anchors.centerIn: parent;text: raw===undefined ? "— °" : Number(raw).toFixed(root.tianji?2:4)+(root.tianji?" °":"");font.pixelSize: 13}
                                }
                                Column {anchors.right: parent.right;anchors.rightMargin: 7;y: 7
                                    Repeater {model: 2
                                        Button {required property int index;property int direction: index===0?1:-1;property real delta: root.tianji ? backend.jogDelta(arm.side,jointRow.index+1,direction) : direction*(spec.name.includes("gripper")?.005:.01);objectName: "jog-"+spec.name+"-"+direction;width: 28;height: 20;enabled: root.canMove && raw!==undefined && raw+delta>=spec.minimum && raw+delta<=spec.maximum
                                            background: Rectangle {color: parent.hovered ? Theme.accentSoft : Theme.surface;border.color: Theme.line;radius: 3}
                                            contentItem: Txt {selectable: false;text: index===0?"▲":"▼";font.pixelSize: 9;horizontalAlignment: Text.AlignHCenter;color: parent.enabled ? Theme.inkMuted : Theme.inkSoft;opacity: parent.enabled?1:.4}
                                            onClicked: {if(root.tianji)backend.jog(arm.side,jointRow.index+1,direction);else backend.jogG2(spec.name,direction)}
                                        }
                                    }
                                }
                            }
                        }
                        Rectangle {visible: root.tianji;width: parent.width;height: 54;radius: 10;color: Theme.surface;border.color: Theme.line
                            Column {x: 9;y: 7;spacing: 2
                                Txt {text: arm.sideTitle.replace("臂","夹爪");font.weight: Font.DemiBold;height: 18}
                                Txt {property var amount: backend.gripperClosure[arm.side];text: amount===null || amount===undefined ? "暂无夹爪反馈" : "闭合 "+Math.round(amount*100)+"%";font.pixelSize: 12;color: Theme.inkSoft;height: 17}
                            }
                            Row {anchors.right: parent.right;anchors.rightMargin: 7;anchors.verticalCenter: parent.verticalCenter;spacing: 6
                                Btn {text: "打开";height: 30;enabled: root.canMove;onClicked: backend.gripper(arm.side,0)}
                                Btn {objectName: "gripper-close-"+arm.side;text: "闭合";height: 30;enabled: root.canMove;onClicked: backend.gripper(arm.side,1)}
                            }
                        }
                    }
                }
            }
        }
    }
    Dialog {
        id: confirm
        objectName: "pose-confirm"
        property string action: "pose"
        parent: Overlay.overlay;anchors.centerIn: parent;width: Math.min(460,app.width-32);modal: true;padding: 20
        background: Rectangle {color: Theme.surface;radius: 12;border.color: Theme.line}
        Overlay.modal: Rectangle {color: "#52000000"}
        contentItem: ColumnLayout {spacing: 18
            Txt {Layout.fillWidth: true;property string poseLabel: (s.poses.find(p=>p.id===s.selectedPose)||{}).name||"";text: confirm.action==="delete"?"删除姿态「"+poseLabel+"」？":root.tianji && s.selectedPose==="zero" ? "将清除已解除的故障并逐臂低速回零。请先释放硬件急停，确认整条回零路径无障碍物。" : "将机械臂移动到「"+poseLabel+"」，请确认运动范围内无人和障碍物。";wrapMode: Text.Wrap;elide: Text.ElideNone}
            RowLayout {Layout.alignment: Qt.AlignRight
                Btn {text: "取消";onClicked: confirm.close()}
                Btn {objectName: "pose-confirm-accept";text: "确认";primary: true;onClicked: {if(confirm.action==="delete")backend.deletePose();else backend.restorePose();confirm.close()}}
            }
        }
    }
    Dialog {
        id: nameDialog;parent: Overlay.overlay;anchors.centerIn: parent;width: Math.min(400,app.width-32);modal: true;padding: 20
        background: Rectangle {color: Theme.surface;radius: 12;border.color: Theme.line}
        Overlay.modal: Rectangle {color: "#52000000"}
        contentItem: ColumnLayout {spacing: 16
            Txt {text: "请输入姿态名称";font.pixelSize: 15;font.weight: Font.DemiBold}
            Field {id: poseName;Layout.fillWidth: true;maximumLength: 50;onAccepted: if(text.trim()){backend.savePose(text);nameDialog.close()}}
            Btn {Layout.alignment: Qt.AlignRight;text: "保存";primary: true;enabled: poseName.text.trim()!=="";onClicked: {backend.savePose(poseName.text);nameDialog.close()}}
        }
    }
}
