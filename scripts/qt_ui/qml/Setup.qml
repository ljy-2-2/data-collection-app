import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Card {
    id: root
    property bool umi: false
    readonly property var robotKeys: backend.robotKeys
    readonly property var robotNames: backend.robotNames
    enabled: !backend.state.busy && !backend.state.closing
    height: content.height+heading.height+24
    Heading {id: heading;width: parent.width;eyebrow: "STEP 01";title: root.umi ? "选择采集配置" : "选择遥操作配置"}
    Column {
        id: content;x: 12;y: heading.height+12;width: parent.width-24;spacing: 12
        UploadTarget {width: parent.width;visible: !root.umi && options.length>0;controlName: "teleopUploadTarget";readOnly: true}
        GridLayout {
            width: parent.width;columns: width<440 ? 1 : 2;columnSpacing: 9;rowSpacing: 9
            ColumnLayout {Layout.fillWidth: true;spacing: 4
                Txt {text: "任务名称";font.pixelSize: 12;color: Theme.inkMuted;font.weight: Font.DemiBold;Layout.preferredHeight: 18}
                Field {id: task;objectName: root.umi ? "umiTask" : "teleopTask";Layout.fillWidth: true;text: root.umi ? backend.state.umiTask : backend.state.task;maximumLength: 120;font.pixelSize: 12;onEditingFinished: backend.setValue(root.umi ? "umiTask" : "task",text)}
            }
            ColumnLayout {Layout.preferredWidth: parent.columns===1 ? parent.width : 180;Layout.maximumWidth: parent.columns===1 ? parent.width : 180;Layout.minimumWidth: parent.columns===1 ? 0 : 180;spacing: 4
                Txt {text: root.umi ? "采集配置" : "机器人型号";font.pixelSize: 12;color: Theme.inkMuted;font.weight: Font.DemiBold;Layout.preferredHeight: 18}
                Select {id: type;objectName: root.umi ? "umiProfile" : "robotType";Layout.fillWidth: true;model: root.umi ? ["双臂","仅左臂","仅右臂"] : root.robotNames;currentIndex: root.umi ? ["dual","single-left","single-right"].indexOf(backend.state.profile) : root.robotKeys.indexOf(backend.state.robot);onActivated: index => {if(root.umi) backend.setValue("profile",["dual","single-left","single-right"][index]);else backend.setValue("robot",root.robotKeys[index])}}
            }
        }
        RowLayout {
            visible: root.umi;width: parent.width;spacing: 7
            Repeater {model: ["相机","编码器","头显"]
                Rectangle {
                    required property string modelData;required property int index
                    Layout.fillWidth: true;height: 55;radius: 10;color: Theme.muted;border.color: Theme.line
                    Image {x: 10;anchors.verticalCenter: parent.verticalCenter;width: 20;height: 20;source: Theme.icon(["Camera","Settings2","Glasses"][index],"accent")}
                    Column {x: 38;y: 9;spacing: 2
                        Txt {text: modelData;font.pixelSize: 12;color: Theme.inkSoft;height: 16}
                        Txt {text: index===2 ? "PICO" : (backend.state.profile==="dual" ? (index===0 ? "3 路" : "2 个") : (index===0 ? "2 路" : "1 个"));font.weight: Font.DemiBold;height: 18}
                    }
                }
            }
        }
        Item {width: parent.width;height: 34
            Btn {objectName: root.umi ? "umiNext" : "teleopNext";anchors.right: parent.right;text: root.umi ? "下一步：检查硬件" : "下一步：遥操作采集";primary: true;iconAfter: true;glyph: "ArrowRight";enabled: task.text.trim()!=="";onClicked: {backend.setValue(root.umi ? "umiTask" : "task",task.text);if(root.umi) backend.umiNext();else backend.configureTeleop(task.text,root.robotKeys[type.currentIndex])}}
        }
    }
}
