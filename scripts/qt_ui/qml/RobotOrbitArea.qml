import QtQuick
MouseArea {
    required property var scene
    anchors.fill: parent
    preventStealing: true
    acceptedButtons: Qt.LeftButton | Qt.RightButton
    property bool panning: false
    property real lastX: 0;property real lastY: 0
    cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
    onPressed: mouse=>{panning=mouse.button===Qt.RightButton;lastX=mouse.x;lastY=mouse.y}
    onPositionChanged: mouse=>{if(pressed){if(panning)scene.pan(mouse.x-lastX,mouse.y-lastY,width,height);else scene.drag(mouse.x-lastX,mouse.y-lastY);lastX=mouse.x;lastY=mouse.y}}
    onWheel: wheel=>scene.changeZoom(wheel.angleDelta.y>0?1.12:.88)
}
