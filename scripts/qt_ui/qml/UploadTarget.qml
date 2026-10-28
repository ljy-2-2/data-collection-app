import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Rectangle {
    id: root
    property var target: backend.state.uploadTarget || {}
    property var options: target.destinations || []
    property bool locked: false
    property bool compact: false
    property bool readOnly: false
    property string controlName: "umiUploadTarget"
    visible: !backend.rawOnly && options.length>0
    implicitHeight: visible ? content.implicitHeight+20 : 0
    height: implicitHeight
    radius: 9;color: Theme.muted;border.color: Theme.line
    RowLayout {id: content;x: 10;y: 10;width: parent.width-20;spacing: 12
        ColumnLayout {Layout.fillWidth: true;Layout.minimumWidth: 0;spacing: 6
            Txt {text: "上传到";font.weight: Font.DemiBold;Layout.fillWidth: true}
            Txt {Layout.fillWidth: true;Layout.minimumWidth: 0;font.pixelSize: 11;color: Theme.inkSoft;wrapMode: Text.Wrap;elide: Text.ElideNone
                text: (root.target.selected==="local"?"上传到当前设备的本地平台":"上传到云端平台")+(root.compact?"":" · "+(root.target.web_url||""))
            }
        }
        Txt {visible: root.readOnly;objectName: root.controlName+"Label";text: root.target.selected==="local"?"本机平台":"云端平台";font.weight: Font.DemiBold;Layout.alignment: Qt.AlignVCenter}
        Select {visible: !root.readOnly;objectName: root.controlName;Layout.preferredWidth: root.compact?110:150;Layout.preferredHeight: 32;Layout.alignment: Qt.AlignVCenter
            model: root.options.map(v=>v.kind==="local"?"本机平台":"云端平台")
            currentIndex: root.options.findIndex(v=>v.kind===root.target.selected)
            enabled: !root.locked && !backend.state.busy
            onActivated: {if(currentIndex>=0)backend.selectUploadTarget(root.options[currentIndex].kind)}
        }
    }
}
