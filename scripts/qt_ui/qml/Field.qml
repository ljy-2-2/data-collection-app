import QtQuick
import QtQuick.Controls
TextField {
    id: control
    implicitHeight: 34
    padding: 9
    topPadding: 0
    bottomPadding: 0
    font.family: Theme.font
    font.pixelSize: 13
    color: Theme.ink
    placeholderTextColor: Theme.inkSoft
    selectionColor: Theme.accentSoft
    selectedTextColor: Theme.ink
    selectByMouse: true
    opacity: enabled ? 1 : .58
    background: Rectangle {
        color: Theme.surface;radius: 7
        border.color: control.activeFocus ? Theme.accentStrong : Theme.lineStrong
        Rectangle {anchors.fill: parent;anchors.margins: -3;radius: 9;color: "transparent";border.width: 2;border.color: "#40f4c520";visible: control.activeFocus}
    }
}
