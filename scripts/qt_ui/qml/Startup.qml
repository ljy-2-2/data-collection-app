import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property string failure: backend.state.startupError
    WheelScroll {
        anchors.fill: parent
        contentHeight: Math.max(height, card.height+48)
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        Card {
            id: card
            width: Math.min(parent.width-48, 440)
            height: content.implicitHeight+56
            x: (parent.width-width)/2
            y: Math.max(24, (root.height-height)/2)
            radius: 14
            ColumnLayout {
                id: content
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 28
                spacing: 18
                Logo {
                    source: backend.assetRoot+"K.png"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredWidth: 48
                    Layout.preferredHeight: 48
                }
                Txt {
                    Layout.fillWidth: true
                    text: root.failure ? "服务启动失败" : "欢迎使用小凯数据采集平台"
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                    elide: Text.ElideNone
                    font.pixelSize: 23
                    font.weight: Font.DemiBold
                }
                Txt {
                    Layout.fillWidth: true
                    text: root.failure ? "请查看下方原因，处理后重新打开应用。" : "正在进入登录界面"
                    color: Theme.inkMuted
                    wrapMode: Text.Wrap
                    elide: Text.ElideNone
                    horizontalAlignment: Text.AlignHCenter
                }
                Rectangle {
                    id: loadingTrack
                    visible: !root.failure
                    Layout.fillWidth: true
                    Layout.preferredHeight: 6
                    radius: 3
                    color: Theme.muted
                    clip: true
                    Rectangle {
                        id: loadingBar
                        width: loadingTrack.width*0.3
                        height: parent.height
                        radius: 3
                        color: Theme.accent
                        NumberAnimation on x {
                            from: -loadingBar.width
                            to: loadingTrack.width
                            duration: 1300
                            loops: Animation.Infinite
                            running: root.visible && !root.failure
                            easing.type: Easing.InOutSine
                        }
                    }
                }
                Rectangle {
                    visible: !!root.failure
                    Layout.fillWidth: true
                    implicitHeight: detail.implicitHeight+20
                    color: Theme.redSoft
                    radius: 8
                    Txt {
                        id: detail
                        x: 10; y: 10; width: parent.width-20
                        text: root.failure
                        color: Theme.red
                        font.pixelSize: 12
                        wrapMode: Text.WrapAnywhere
                        elide: Text.ElideNone
                    }
                }
                Btn {
                    visible: !!root.failure
                    Layout.fillWidth: true
                    text: "退出应用"
                    onClicked: backend.closeRequested()
                }
            }
        }
    }
}
