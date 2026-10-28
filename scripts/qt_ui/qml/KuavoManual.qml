import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Card {
    id: root
    objectName: "kuavoManualCard"
    property var s: backend.state
    property var k: s.kuavo||({})
    property var manual: k.manual||({})
    property var telemetry: manual.telemetry||({})
    property var joints: manual.joints||[]
    property bool ready: manual.state==="ready" && !!manual.fresh
    property bool canMove: ready && !manual.busy && !s.busy
    property real step: [0.1,0.5,1][stepSelect.currentIndex]
    property string held: ""
    property string requestError: s.kuavoManualError||""
    width: parent.width
    height: content.height+24
    function send(value) { backend.kuavoManualInput(value) }
    function release() { if(held!==""){held="";send({action:"base",direction:"stop"})} }
    function press(direction) { if(!canMove || held!=="")return;held=direction;send({action:"base",direction:direction}) }
    function act(value) { if(canMove && held==="")send(value) }
    onCanMoveChanged: if(!canMove)release()
    onVisibleChanged: if(!visible)release()
    Connections {target:app;function onActiveChanged(){if(!app.active)root.release()}}
    Component.onDestruction: release()
    Timer {interval:200;repeat:true;running:root.visible && !!root.manual.active;onTriggered:root.send({action:"heartbeat"})}
    Timer {interval:80;repeat:true;running:root.held!=="";onTriggered:root.send({action:"base",direction:root.held})}
    Connections {target:backend;function onKuavoManualInputFailed(){root.held=""}}
    Column {
        id:content;x:12;y:12;width:parent.width-24;spacing:12
        RowLayout {width:parent.width;spacing:9
            Txt {text:"手动控制";font.pixelSize:15;font.weight:Font.DemiBold}
            Rectangle {width:60;height:22;radius:5;color:Theme.amberSoft;Txt {anchors.centerIn:parent;text:"真实运动";font.pixelSize:11;color:Theme.amber}}
            Item {Layout.fillWidth:true}
            Txt {text:"步长";font.pixelSize:12}
            Select {id:stepSelect;objectName:"kuavoManualStep";Layout.preferredWidth:76;model:["0.1°","0.5°","1°"];currentIndex:2}
            Btn {objectName:"kuavoManualEnter";visible:!root.manual.active;text:"进入手动控制";enabled:!!root.manual.available && !s.busy && (!k.active || k.state==="ready");onClicked:backend.kuavoManual("start")}
            Btn {objectName:"kuavoManualExit";visible:!!root.manual.active;text:root.manual.state==="stopping"?"正在退出…":"结束手动控制";enabled:root.manual.state!=="stopping";onClicked:{root.release();backend.kuavoManual("stop")}}
        }
        Rectangle {width:parent.width;height:1;color:Theme.line}
        GridLayout {width:parent.width;columns:width<740?1:3;columnSpacing:9;rowSpacing:9
            Repeater {model:["left","center","right"]
                Rectangle {
                    id:group
                    required property string modelData
                    property bool center:modelData==="center"
                    Layout.fillWidth:true;Layout.preferredWidth:1;Layout.alignment:Qt.AlignTop
                    Layout.preferredHeight:groupContent.height+12
                    color:Theme.muted;border.color:Theme.line;radius:10
                    Column {
                        id:groupContent;width:parent.width;spacing:5
                        Repeater {
                            model:group.center?["body","head"]:[group.modelData]
                            Column {
                                id:section
                                required property string modelData
                                width:parent.width;spacing:6
                                Item {width:parent.width;height:36
                                    Txt {x:10;y:8;height:20;text:section.modelData==="left"?"左臂":section.modelData==="right"?"右臂":section.modelData==="body"?"躯干":"头部";font.weight:Font.DemiBold}
                                    Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:Theme.line}
                                }
                                Column {x:6;width:parent.width-12;spacing:5
                                    Repeater {
                                        model:root.joints.filter(j=>j.group===section.modelData)
                                        Rectangle {
                                            id:jointRow
                                            required property var modelData
                                            property var angle:(root.telemetry.joints||{})[modelData.name]
                                            property bool hasAngle:typeof angle==="number" && isFinite(angle)
                                            property real degrees:hasAngle?angle*180/Math.PI:0
                                            width:parent.width;height:54;radius:10;color:Theme.surface;border.color:Theme.line
                                            Column {x:9;y:7;width:parent.width-124;spacing:2
                                                Txt {width:parent.width;height:18;text:jointRow.modelData.label;font.weight:Font.DemiBold}
                                                Txt {width:parent.width;height:17;text:Math.round(jointRow.modelData.min_deg)+"° ～ "+Math.round(jointRow.modelData.max_deg)+"°";font.pixelSize:12;color:Theme.inkSoft}
                                            }
                                            Rectangle {anchors.right:parent.right;anchors.rightMargin:34;y:7;width:72;height:40;radius:7;color:Theme.muted;border.color:Theme.line
                                                Txt {anchors.centerIn:parent;text:jointRow.hasAngle?jointRow.degrees.toFixed(2)+" °":"— °";font.pixelSize:13;color:root.ready?Theme.ink:Theme.inkSoft}
                                            }
                                            Column {anchors.right:parent.right;anchors.rightMargin:7;y:7
                                                Repeater {model:2
                                                    Button {
                                                        required property int index
                                                        property int direction:index===0?1:-1
                                                        width:28;height:20
                                                        text:index===0?"▲":"▼"
                                                        enabled:root.canMove && root.held==="" && jointRow.hasAngle && jointRow.degrees+direction*root.step>=jointRow.modelData.min_deg && jointRow.degrees+direction*root.step<=jointRow.modelData.max_deg
                                                        background:Rectangle {color:parent.hovered?Theme.accentSoft:Theme.surface;border.color:Theme.line;radius:3}
                                                        contentItem:Txt {selectable:false;text:parent.text;font.pixelSize:9;horizontalAlignment:Text.AlignHCenter;color:parent.enabled?Theme.inkMuted:Theme.inkSoft;opacity:parent.enabled?1:.4}
                                                        onClicked:root.act({action:"joint",joint:jointRow.modelData.name,delta_deg:direction*root.step})
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        Rectangle {visible:!group.center;x:6;width:parent.width-12;height:visible?54:0;radius:10;color:Theme.surface;border.color:Theme.line
                            Column {x:9;y:7;spacing:2
                                Txt {text:group.modelData==="left"?"左夹爪":"右夹爪";font.weight:Font.DemiBold;height:18}
                                Txt {property var f:((root.telemetry.grippers||{})[group.modelData]||{}).closure_fraction;text:typeof f==="number"?"闭合 "+Math.round(f*100)+"%":"暂无夹爪反馈";font.pixelSize:12;color:Theme.inkSoft;height:17}
                            }
                            Row {anchors.right:parent.right;anchors.rightMargin:7;anchors.verticalCenter:parent.verticalCenter;spacing:6
                                Btn {text:"打开";height:30;enabled:root.canMove && root.held==="";onClicked:root.act({action:"gripper",side:group.modelData,closed:false})}
                                Btn {text:"闭合";height:30;enabled:root.canMove && root.held==="";onClicked:root.act({action:"gripper",side:group.modelData,closed:true})}
                            }
                        }
                        Column {visible:group.center;width:parent.width;spacing:12;height:visible?implicitHeight:0
                            Item {width:parent.width;height:36
                                Txt {x:10;y:8;height:20;text:"底盘";font.weight:Font.DemiBold}
                                Rectangle {anchors.bottom:parent.bottom;width:parent.width;height:1;color:Theme.line}
                            }
                            Loader {anchors.horizontalCenter:parent.horizontalCenter;width:220;height:220;active:group.center
                                sourceComponent:KuavoBasePad {canMove:root.canMove;held:root.held
                                    onDirectionPressed:direction=>root.press(direction)
                                    onDirectionReleased:root.release()
                                }
                            }
                        }
                    }
                }
            }
        }
        Txt {width:parent.width;visible:text!=="";wrapMode:Text.Wrap;color:Theme.amber;font.pixelSize:12;text:root.requestError || root.manual.error || (root.manual.state==="starting"?"正在核验并连接手动控制…":root.manual.available?"":root.manual.reason||"检查设备后连接手动控制")}
    }
}
