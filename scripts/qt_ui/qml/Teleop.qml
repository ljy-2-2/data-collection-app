import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Collection.Native 1.0
Column {
    id: root
    property var s: backend.state
    property bool tianji: s.robot==="tianji"
    property var status: tianji ? s.tianji : s.g2
    property bool running: tianji ? status.state==="running" : (status.session||{}).state==="running"
    property bool moving: tianji ? status.state==="moving" : (status.motion||{}).state!=="idle"
    property bool uncertain: tianji ? ["unknown","error"].indexOf(status.state)>=0 : (status.session||{}).state==="unknown"
    property bool manualMotion: moving || s.busy==="move"
    property bool teleopStop: !manualMotion && (running || uncertain)
    property bool cameraSettingsBusy: tianji && (!!status.camera_settings_busy || Object.values(backend.teleopCameraSettings).some(v=>v.busy))
    property bool locked: !!s.busy || running || moving || uncertain || !!(status.collection||{}).busy || !!(status.collection||{}).recording || cameraSettingsBusy
    property bool live: tianji ? !!((status.joints||{}).left && (status.joints||{}).left.length) : !!s.preview.available
    property bool canMove: !locked && (tianji ? live && s.paramSaved : s.preview.available && status.devices.some(d=>d.id==="robot" && d.status==="connected"))
    property bool canRecover: tianji && !s.busy && !s.paramSaving && !cameraSettingsBusy && s.paramSaved && ["idle","error"].indexOf(status.state)>=0 && !(status.collection||{}).busy && !(status.collection||{}).recording
    property var collection: status.collection||{}
    spacing: 12
    Card {
        id: control
        width: parent.width;height: setupGrid.height+footer.height+1
        GridLayout {
            id: setupGrid
            width: parent.width;columns: app.width<=1180 ? 1 : 2;columnSpacing: 0;rowSpacing: 0
            Item {
                Layout.fillWidth: true;Layout.preferredWidth: root.tianji ? 620 : 460;Layout.preferredHeight: modeColumn.height+24
                Column {id: modeColumn;x: 12;y: 12;width: parent.width-24;spacing: 12
                    RowLayout {width: parent.width;height: 20;spacing: 5
                        Txt {text: "01";font.pixelSize: 11;color: Theme.accentStrong;Layout.preferredWidth: 22}
                        Txt {text: root.tianji ? "上传曲线" : "遥操作模式";font.weight: Font.DemiBold}
                        Txt {visible: root.tianji && !s.paramSaved;text: s.paramSaving ? "自动保存中…" : "参数待保存";font.pixelSize: 11;color: Theme.inkMuted}
                        Item {Layout.fillWidth: true}
                    }
                    GridLayout {
                        width: parent.width;columns: app.width<780 ? 2 : 3;columnSpacing: 8;rowSpacing: 8
                        Select {id: mode;Layout.preferredWidth: root.tianji ? 68 : 112;model: root.tianji ? ["双臂","左臂","右臂"] : ["双臂遥操作"];enabled: !root.locked;currentIndex: root.tianji ? ["dual","left","right"].indexOf(s.uploadMode) : 0;font.pixelSize: 12;onActivated: if(root.tianji) backend.setValue("uploadMode",["dual","left","right"][currentIndex])}
                        GridLayout {
                            Layout.fillWidth: true;Layout.row: app.width<780 ? 1 : 0;Layout.column: app.width<780 ? 0 : 1;Layout.columnSpan: app.width<780 ? 2 : 1
                            columns: app.width<480 ? 1 : root.tianji ? 3 : 2;columnSpacing: 6
                            Repeater {model: root.tianji ? 3 : 2
                                NumberField {required property int index;Layout.fillWidth: true;spec: root.tianji ? backend.fields[index] : {key:index===0?"g2Scale":"g2Speed",label:index===0?"幅度":"速度",unit:"×",min:.1,max:1,step:.1};value: root.tianji ? s.parameters[spec.key] : s[spec.key];showRange: root.tianji;enabled: !root.locked;onEdited: number=>{if(root.tianji)backend.setParameter(spec.key,number);else backend.setValue(spec.key,number)}}
                            }
                        }
                        Btn {visible: root.tianji;Layout.row: 0;Layout.column: app.width<780 ? 1 : 2;objectName: "otherParameters";text: "其它参数";glyph: "Settings2";textSize: 12;enabled: !root.locked;onClicked: parameterDialog.open()}
                    }
                }
            }
            Item {Layout.fillWidth: true;Layout.preferredWidth: 375;Layout.preferredHeight: 102
                Rectangle {width: setupGrid.columns===1 ? parent.width : 1;height: setupGrid.columns===1 ? 1 : parent.height;color: Theme.line}
                Devices {x: 12;y: 12;width: parent.width-24;items: root.status.devices;checking: s.busy==="check" || root.status.state==="checking"}
            }
        }
        Rectangle {
            id: footer;y: setupGrid.height;width: parent.width;height: footerFlow.implicitHeight+12;color: Theme.muted;radius: 0
            Rectangle {width: parent.width;height: 1;color: Theme.line}
            Flow {id: footerFlow;x: 12;y: 6;width: parent.width-24;spacing: 6;layoutDirection: Qt.RightToLeft
                Btn {
                    objectName: "teleopStartStop"
                    text: root.teleopStop ? (root.tianji ? "停止控制" : "结束遥操作") : (root.tianji ? "开始遥操作" : "开始遥操作采集")
                    glyph: root.teleopStop ? "Square" : "Play"
                    primary: true; danger: root.teleopStop
                    busy: !root.manualMotion && ["start","stop"].indexOf(s.busy)>=0
                    enabled: !root.manualMotion && ((root.teleopStop || s.busy==="start") ? s.busy!=="stop" : !root.locked && root.status.devices.filter(d=>d.status==="connected").length===3 && (!root.tianji || (s.paramSaved && !s.paramSaving)))
                    onClicked: backend.teleop(root.teleopStop || s.busy==="start" ? "stop" : "start")
                }
                Btn {objectName: "teleopCheck";text: "检查设备";glyph: "RefreshCw";busy: s.busy==="check";enabled: !root.locked && !s.paramSaving;onClicked: backend.teleop("check")}
                Row {visible: root.tianji;spacing: 14;height: 34;layoutDirection: Qt.LeftToRight
                    Txt {height: 34;text: "本次已完成  "+(root.collection.completed||0)+" Episode";font.pixelSize: 12;color: Theme.inkMuted}
                    Txt {height: 34;text: root.collection.recording ? "● 采集中" : root.collection.busy ? "正在保存" : root.running ? "等待手柄 A" : "未开始遥操作";color: root.collection.recording ? Theme.red : Theme.inkMuted;font.pixelSize: 12}
                }
                Row {visible: !root.tianji;spacing: 8;height: 34;layoutDirection: Qt.LeftToRight
                    Txt {height: 34;text: "额外信息保存：";font.pixelSize: 12;color: Theme.inkMuted}
                    Repeater {model: ["头部深度","左手深度","右手深度"]
                        Tick {required property string modelData;required property int index;text: modelData;font.pixelSize: 12;anchors.verticalCenter: parent.verticalCenter;enabled: !root.running;property string key: ["head_depth","hand_left_depth","hand_right_depth"][index];checked: s.extraCameras.indexOf(key)>=0;onClicked: {let list=s.extraCameras.slice();if(checked)list.push(key);else list=list.filter(k=>k!==key);backend.setValue("extraCameras",list)}}
                    }
                }
            }
        }
    }
    GridLayout {
        id: visualGrid;objectName: "teleopVisualGrid"
        width: parent.width;columns: width<1080 ? 1 : 2;columnSpacing: 12;rowSpacing: 12
        Card {
            objectName: "teleopRobotCard"
            Layout.fillWidth: true;Layout.preferredWidth: 444;Layout.minimumWidth: visualGrid.columns===2 ? 340 : 0;Layout.maximumWidth: visualGrid.columns===2 ? 504 : Infinity;Layout.preferredHeight: visualGrid.columns===2 ? cameraCard.implicitHeight : 340
            Heading {width: parent.width;title: "机器人姿态";height: 51}
            Txt {anchors.right: parent.right;anchors.rightMargin: 14;y: 17;text: "● "+(root.live ? "遥测在线" : "无遥测");font.pixelSize: 11;color: root.live ? Theme.green : Theme.inkSoft}
            Rectangle {
                objectName: "teleopRobotStage"
                x: 10;y: 61;width: parent.width-20;height: parent.height-y-10;radius: 10;border.color: Theme.line;color: Theme.stage;clip: true
                Loader {anchors.fill: parent;anchors.margins: 1;active: root.tianji;source: "RobotView.qml"}
                G2View {id: g2;visible: !root.tianji;anchors.fill: parent;anchors.margins: 1;dark: Theme.dark;live: root.live;values: {let v={};backend.g2Joints.forEach(j=>v[j.name]=s.preview.joint_positions[j.name]===undefined?j.default:s.preview.joint_positions[j.name]);return v}}
                MouseArea {objectName: "g2OrbitArea";anchors.fill: parent;enabled: !root.tianji;preventStealing: true;property real px: 0;property real py: 0;cursorShape: pressed?Qt.ClosedHandCursor:Qt.OpenHandCursor;onPressed: m=>{px=m.x;py=m.y};onPositionChanged: m=>{if(pressed){g2.drag(m.x-px,m.y-py);px=m.x;py=m.y}};onWheel: w=>g2.changeZoom(w.angleDelta.y>0?1.12:.88)}
                Row {visible: !root.tianji;anchors.right: parent.right;anchors.top: parent.top;anchors.margins: 8;spacing: 1
                    Btn {text: "−";width: 28;height: 28;onClicked: g2.changeZoom(.88)}
                    Btn {text: "+";width: 28;height: 28;onClicked: g2.changeZoom(1.12)}
                }
            }
        }
        TeleopCameras {
            id: cameraCard
            Layout.fillWidth: true;Layout.preferredWidth: 716;Layout.minimumWidth: visualGrid.columns===2 ? 700 : 0;Layout.preferredHeight: implicitHeight
            tianji: root.tianji;status: root.status;previewStatus: s.preview;locked: root.locked
            active: root.visible && s.page==="teleop" && s.teleopStep===2
        }
    }
    Manual {width: parent.width;tianji: root.tianji;canMove: root.canMove;canRecover: root.canRecover;live: root.live;locked: root.locked}
    Dialog {
        id: parameterDialog;objectName: "parameterDialog";parent: Overlay.overlay;anchors.centerIn: parent
        width: Math.min(560,app.width-32);height: Math.min(app.height-48,parameterContent.implicitHeight+40);modal: true;padding: 20;closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle {color: Theme.surface;radius: 12;border.color: Theme.line}
        Overlay.modal: Rectangle {color: "#52000000"}
        contentItem: WheelScroll {contentWidth: width;contentHeight: parameterContent.implicitHeight;clip: true;boundsBehavior: Flickable.StopAtBounds
          ScrollBar.vertical: ScrollBar {interactive: false}
          ColumnLayout {id: parameterContent;width: parent.width;spacing: 18
            RowLayout {Layout.fillWidth: true
                Txt {text: "其它参数";font.pixelSize: 16;font.weight: Font.DemiBold;Layout.fillWidth: true}
                Btn {text: "";glyph: "X";width: 30;height: 30;ghost: true;enabled: !s.busy;onClicked: parameterDialog.close()}
            }
            GridLayout {Layout.fillWidth: true;columns: app.width<420 ? 1 : 2;columnSpacing: 14;rowSpacing: 14
                Repeater {model: backend.fields.length-3
                    NumberField {required property int index;Layout.fillWidth: true;spec: backend.fields[index+3];value: s.parameters[spec.key];showRange: false;arrows: false;enabled: !root.locked;onEdited: number=>backend.setParameter(spec.key,number)}
                }
            }
            Txt {text: s.paramSaved ? "参数已自动保存" : s.paramSaving ? "正在自动保存…" : "自动保存未完成，可通过检查设备重试";font.pixelSize: 12;color: Theme.inkMuted}
          }
        }
    }
}
