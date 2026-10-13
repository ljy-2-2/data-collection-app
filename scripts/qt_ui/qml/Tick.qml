import QtQuick
import QtQuick.Controls
CheckBox {
    id: control
    padding: 0
    spacing: 7
    implicitHeight: 22
    implicitWidth: indicator.width+spacing+label.implicitWidth
    font.family: Theme.font
    font.pixelSize: 13
    opacity: enabled ? 1 : .58
    indicator: Rectangle {
        width: 16;height: 16;y: (control.height-height)/2;radius: 2
        color: control.checked ? Theme.accentStrong : Theme.surface
        border.color: control.activeFocus ? Theme.accentStrong : Theme.lineStrong
        Image {anchors.fill: parent;anchors.margins: 1;visible: control.checked;source: Theme.icon("Check","ink")}
    }
    contentItem: Txt {id: label;text: control.text;leftPadding: control.indicator.width+control.spacing;color: Theme.inkMuted;font.pixelSize: control.font.pixelSize}
}
