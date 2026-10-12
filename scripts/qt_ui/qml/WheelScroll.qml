import QtQuick

Flickable {
    id: root
    interactive: false
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    // Qt 6.8 disables wheel input along with dragging when interactive is false.
    // Handle unconsumed wheel events behind the content, leaving controls free
    // to receive clicks, slider drags, text selection and model interactions.
    MouseArea {
        parent: root
        anchors.fill: parent
        z: -1
        acceptedButtons: Qt.NoButton
        onWheel: event => {
            var delta = event.pixelDelta.y !== 0
                ? event.pixelDelta.y : event.angleDelta.y / 120 * 60
            if (delta === 0) {
                event.accepted = false
                return
            }
            var bottom = root.originY + Math.max(0, root.contentHeight - root.height)
            root.contentY = Math.max(root.originY, Math.min(bottom, root.contentY - delta))
            event.accepted = true
        }
    }
}
