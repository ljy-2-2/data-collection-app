import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Rectangle {
    id: root
    property var target: backend.state.uploadTarget || {}
    property var options: target.destinations || []
    property bool locked: false
    property bool compact: false
    property string controlName: "umiUploadTarget"
    visible: options.length>0
    implicitHeight: visible ? content.implicitHeight+20 : 0
    height: implicitHeight
    radius: 9;color: Theme.muted;border.color: Theme.line
    ColumnLayout {id: content;x: 10;y: 10;width: parent.width-20;spacing: 6
        RowLayout {Layout.fillWidth: true;spacing: 10
            Txt {text: "上传到";font.weight: Font.DemiBold;Layout.fillWidth: true}
            Select {objectName: root.controlName;Layout.preferredWidth: root.compact?130:190;height: 32
                model: root.options.map(v=>v.kind==="local"?"本机平台":"云端平台")
                currentIndex: root.options.findIndex(v=>v.kind===root.target.selected)
                enabled: !root.locked && !backend.state.busy
                onActivated: {if(currentIndex>=0)backend.selectUploadTarget(root.options[currentIndex].kind)}
            }
        }
        Txt {Layout.fillWidth: true;font.pixelSize: 11;color: Theme.inkSoft;wrapMode: Text.Wrap;elide: Text.ElideNone
            text: (root.target.selected==="local"?"上传到当前设备的本地平台":"上传到云端平台")+(root.compact?"":" · "+(root.target.web_url||""))
        }
    }
}
