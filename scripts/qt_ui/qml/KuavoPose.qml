import QtQuick
Item {
    id: root
    Loader {
        anchors.fill:parent
        active:GraphicsInfo.api!==GraphicsInfo.Software
        sourceComponent:Component { RobotView {scene:backend.kuavoScene;orbitObjectName:"kuavoOrbitArea"} }
    }
    Txt {
        anchors.centerIn:parent;width:parent.width-30;wrapMode:Text.Wrap;horizontalAlignment:Text.AlignHCenter
        visible:GraphicsInfo.api===GraphicsInfo.Software
        text:"当前渲染模式不支持三维模型";color:Theme.inkMuted
    }
}
