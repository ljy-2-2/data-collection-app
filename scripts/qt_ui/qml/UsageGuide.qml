import QtQuick
import QtQuick.Controls

Item {
    id: root
    readonly property bool previewVisible: preview.visible
    property url source
    property real viewportWidth: 1280
    property real viewportHeight: 800
    implicitWidth: label.implicitWidth
    implicitHeight: 30
    onSourceChanged: preview.close()
    onVisibleChanged: { if (!visible) preview.close() }

    Row {
        id: label
        anchors.verticalCenter: parent.verticalCenter
        spacing: 7
        Image {width: 19;height: 19;anchors.verticalCenter: parent.verticalCenter;source: Theme.icon("HelpCircle")}
        Txt {text: "使用说明";font.pixelSize: 12;color: Theme.inkMuted;height: 30}
    }
    HoverHandler {
        id: labelHover
        onHoveredChanged: { if (hovered) preview.open() }
    }
    Timer {
        interval: 180
        running: preview.opened && !labelHover.hovered && !previewHover.hovered
        onTriggered: preview.close()
    }
    Popup {
        id: preview
        objectName: "usageGuidePreview"
        parent: root
        x: root.width-width
        y: root.height+4
        width: Math.max(100, Math.min(900, root.viewportWidth-32))
        height: Math.max(80, Math.min(root.viewportHeight-120, (width-18)*picture.sourceSize.height/Math.max(1,picture.sourceSize.width)+18))
        padding: 9
        modal: false
        focus: false
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle {color: Theme.surface;radius: 12;border.color: Theme.line}
        contentItem: Image {
            id: picture
            source: root.source
            fillMode: Image.PreserveAspectFit
            mipmap: true
            HoverHandler {id: previewHover}
        }
    }
}
