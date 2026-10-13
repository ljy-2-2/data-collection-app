import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
Column {
    id: root
    property var items: []
    property bool checking: false
    width: parent.width;spacing: 8
    RowLayout {width: parent.width;height: 20;spacing: 5
        Txt {text: "02";font.pixelSize: 11;color: Theme.accentStrong;Layout.preferredWidth: 22}
        Txt {text: "所需设备";font.weight: Font.DemiBold}
        Item {Layout.fillWidth: true}
        Txt {text: root.items.filter(d=>d.status==="connected").length+"/3";font.pixelSize: 11;color: Theme.inkSoft}
    }
    RowLayout {
        width: parent.width;spacing: 5
        Repeater {model: 3
            Rectangle {required property int index;property var device: root.items[index] || {};Layout.fillWidth: true;height: 50;color: Theme.surface;radius: 7;border.color: device.status==="connected" ? Theme.green : Theme.line
                Column {anchors.fill: parent;anchors.margins: 6;spacing: 0
                    Txt {width: parent.width;text: device.name||["机器人","Galaxis VR","Pico 设备"][index];font.pixelSize: 12;font.weight: Font.DemiBold;height: 18}
                    Txt {text: root.checking ? "检测中" : device.status==="connected" ? "已连接" : "未连接";font.pixelSize: 11;height: 16;color: root.checking ? Theme.amber : device.status==="connected" ? Theme.green : Theme.red}
                }
                HoverHandler {id: hover}
                ToolTip.visible: hover.hovered && !!device.detail
                ToolTip.text: device.detail||""
            }
        }
    }
}
