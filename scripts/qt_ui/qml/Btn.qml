import QtQuick
import QtQuick.Controls
Button {
    id: control
    property bool primary: false
    property bool iconAfter: false
    property bool ghost: false
    property bool danger: false
    property bool busy: false
    property string glyph: ""
    property int textSize: 13
    property color textColor: primary ? (danger ? "white" : "#292929") : danger ? Theme.red : ghost ? Theme.inkMuted : Theme.ink
    implicitHeight: 34
    implicitWidth: content.implicitWidth + 22
    padding: 0
    opacity: enabled ? 1 : .58
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    Accessible.name: text
    background: Rectangle {
        radius: 7
        border.color: control.activeFocus ? Theme.accentStrong : control.primary ? (control.danger ? "#a61f17" : Theme.accent) : control.ghost ? "transparent" : control.hovered ? Theme.accent : Theme.lineStrong
        color: control.primary ? (control.danger ? "#bb2b20" : control.hovered ? "#ddb00f" : Theme.accent) : control.hovered ? Theme.muted : control.ghost ? "transparent" : Theme.surface
        Rectangle {anchors.fill: parent; anchors.margins: -3; radius: 9; color: "transparent"; border.width: 2; border.color: "#40f4c520"; visible: control.activeFocus}
    }
    contentItem: Item {
        Row {
            id: content
            anchors.centerIn: parent
            spacing: 6
            layoutDirection: control.iconAfter ? Qt.RightToLeft : Qt.LeftToRight
            Image {width: 16; height: 16; anchors.verticalCenter: parent.verticalCenter; visible: control.glyph!=="" || control.busy; source: !control.glyph && !control.busy ? "" : control.primary && !control.danger ? "../icons/"+(control.busy ? "LoaderCircle" : control.glyph)+"-ink.svg" : Theme.icon(control.busy ? "LoaderCircle" : control.glyph,"ink"); sourceSize.width: 32; sourceSize.height: 32; RotationAnimator on rotation {running: control.busy; from: 0; to: 360; duration: 1000; loops: Animation.Infinite}}
            Txt {selectable: false;text: control.text; color: control.textColor; font.pixelSize: control.textSize; font.weight: Font.Medium; height: 20}
        }
    }
}
