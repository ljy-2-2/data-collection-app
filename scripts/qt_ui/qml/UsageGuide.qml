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
        width: Math.max(100, Math.min(600, root.viewportWidth-32))
        height: Math.max(80, Math.min((root.viewportHeight-120)*2/3, root.source.toString() ? (width-26)*picture.sourceSize.height/Math.max(1,picture.sourceSize.width)+70 : 118))
        padding: 12
        modal: false
        focus: false
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Item {
            Rectangle {x: 0;y: 5;width: parent.width;height: parent.height;radius: 12;color: "#000000";opacity: Theme.dark ? .25 : .10}
            Rectangle {anchors.fill: parent;radius: 12;color: Theme.surface;border.color: Theme.lineStrong}
            Rectangle {anchors.left: parent.left;anchors.right: parent.right;anchors.bottom: parent.bottom;anchors.margins: 9;height: 2;radius: 1;color: Theme.lineStrong}
        }
        contentItem: Item {
            HoverHandler {id: previewHover}
            Txt {text: "使用说明";height: 22;font.pixelSize: 12;font.weight: Font.DemiBold;color: Theme.inkMuted}
            Rectangle {y: 27;width: parent.width;height: 1;color: Theme.line}
            Image {
                id: picture
                objectName: "usageGuideImage"
                y: 38;width: parent.width;height: Math.max(0,parent.height-46)
                source: root.source
                fillMode: Image.PreserveAspectFit
                mipmap: true
            }
        }
    }
}
