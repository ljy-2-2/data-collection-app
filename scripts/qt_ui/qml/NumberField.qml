import QtQuick
import QtQuick.Controls
Item {
    id: root
    property var spec
    property real value: 0
    property bool showRange: true
    property bool arrows: true
    signal edited(real number)
    function pendingValue() {
        let n=Number(input.text)
        return input.text.trim() && Number.isFinite(n) && n>=spec.min && n<=spec.max ? n : value
    }
    function commit() {let n=pendingValue();input.text=String(n);edited(n)}
    implicitHeight: showRange ? 66 : 55
    implicitWidth: 130
    onValueChanged: if (!input.activeFocus) input.text=String(value)
    Txt {text: root.spec.label;font.pixelSize: root.showRange ? 11 : 12;color: Theme.inkMuted;height: 18;width: parent.width}
    Rectangle {
        y: 21;width: parent.width;height: root.showRange ? 32 : 34;color: Theme.surface;radius: 7;border.color: input.activeFocus ? Theme.accentStrong : Theme.lineStrong
        TextField {
            id: input
            anchors.left: parent.left;anchors.right: unit.left;height: parent.height;padding: 5
            text: String(root.value);font.family: Theme.font;font.pixelSize: 12;color: Theme.ink
            selectByMouse: true;inputMethodHints: Qt.ImhFormattedNumbersOnly
            validator: DoubleValidator {bottom: root.spec.min;top: root.spec.max;decimals: 6;notation: DoubleValidator.StandardNotation;locale: "C"}
            background: Item {}
            onEditingFinished: root.commit()
        }
        Txt {id: unit;anchors.right: buttons.left;anchors.rightMargin: 3;anchors.verticalCenter: parent.verticalCenter;text: root.spec.unit;font.pixelSize: 10;color: Theme.inkMuted}
        Column {
            id: buttons;anchors.right: parent.right;width: root.arrows ? 19 : 2;height: parent.height
            Repeater {model: root.arrows ? 2 : 0
                Button {required property int index;width: 19;height: 16;enabled: root.enabled && (index===0 ? root.value<root.spec.max : root.value>root.spec.min)
                    background: Rectangle {color: parent.hovered ? Theme.accentSoft : Theme.muted;border.color: Theme.line}
                    contentItem: Image {source: Theme.icon(index===0 ? "ChevronUp" : "ChevronDown");sourceSize.width: 12;sourceSize.height: 12;fillMode: Image.Pad;opacity: parent.enabled ? 1 : .35}
                    onClicked: {root.edited(Math.round(Math.max(root.spec.min,Math.min(root.spec.max,root.value+(index===0?1:-1)*root.spec.step))*10000)/10000)}
                }
            }
        }
    }
    Txt {visible: root.showRange;y: 56;width: parent.width;font.pixelSize: 10;color: Theme.inkMuted;text: "范围 "+root.spec.min+"–"+root.spec.max+" "+root.spec.unit}
}
