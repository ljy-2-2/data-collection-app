import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Item {
    id: root
    property string cameraKey: ""
    property string cameraName: "相机"
    property bool sidePanel: false
    property string scope: "umi"
    readonly property bool compact: scope === "tianji"
    property bool available: false
    property bool editable: true
    property var details: scope === "tianji" ? ((backend.teleopCameraSettings[cameraKey] || {}).resolution || {}) : (backend.umiCameraResolution[cameraKey] || {})
    property var choices: details.options || []
    implicitHeight: compact ? (sidePanel ? 50 : 28)+(note.text ? 16 : 0) : sidePanel ? 82 : 58
    function refresh() { if (available && cameraKey && (scope !== "tianji" || editable)) {if (scope === "tianji") backend.loadTeleopCameraSettings(cameraKey);else backend.loadCameraResolution(cameraKey)} }
    onCameraKeyChanged: refresh()
    onAvailableChanged: {if (scope !== "tianji" || !details.loaded) refresh()}
    onEditableChanged: {if (editable && !details.loaded && !details.busy && !details.error) refresh()}
    function refreshIfNeeded() {if (available && !details.selected && !details.busy && !details.error) refresh()}
    onDetailsChanged: Qt.callLater(refreshIfNeeded)
    Component.onCompleted: refresh()
    Txt {visible: root.sidePanel;x: 8;y: 5;text: "录制分辨率";font.pixelSize: root.compact ? 10 : 11;color: Theme.inkMuted}
    RowLayout {x: 8;y: root.sidePanel ? (root.compact ? 22 : 26) : 4;width: parent.width-16;height: root.compact ? 22 : 29;spacing: 5
        Txt {visible: !root.sidePanel;text: "录制分辨率";font.pixelSize: root.compact ? 10 : 11;color: Theme.inkMuted}
        Select {
            objectName: "cameraResolutionSelect";Layout.fillWidth: true;Layout.preferredHeight: root.compact ? 22 : 29;font.pixelSize: root.compact ? 10 : 11
            optionHeight: root.compact ? 24 : 34
            model: root.choices.map(m=>m.label)
            currentIndex: root.details.selected ? root.choices.findIndex(m=>m.id===root.details.selected.id) : -1
            displayText: root.details.selected ? root.details.selected.label : "请选择分辨率"
            enabled: root.available && root.editable && !!root.details.editable && !root.details.busy && root.choices.length>0
            Accessible.name: root.cameraName+"采集分辨率"
            ToolTip.visible: !root.compact && hovered
            ToolTip.text: displayText
            onActivated: {if (root.scope === "tianji") backend.setTeleopCameraResolution(root.cameraKey,root.choices[currentIndex].id);else backend.setCameraResolution(root.cameraKey,root.choices[currentIndex].id)}
        }
        Btn {text: "刷新";textSize: root.compact ? 9 : 10;Layout.preferredWidth: root.compact ? 32 : 38;Layout.preferredHeight: root.compact ? 22 : 27
            enabled: root.available && (root.scope!=="tianji"||root.editable) && !root.details.busy;onClicked: root.refresh()}
    }
    Txt {id: note;visible: !!text;x: 8;y: root.compact ? (root.sidePanel ? 48 : 28) : root.sidePanel ? 60 : 36;width: parent.width-16;height: 16;font.pixelSize: 10
        text: root.compact ? (root.details.error || "") : root.details.busy?"正在读取 / 验证实际画面…":root.details.error?root.details.error:!root.available?"等待相机连接":root.editable?"按所选分辨率录制 · 采集前验证帧率":"采集期间锁定；返回检查硬件可修改"
        color: root.details.error?Theme.red:Theme.inkSoft
        HoverHandler {id: hover}
        ToolTip.visible: !root.compact && hover.hovered && !!root.details.error
        ToolTip.text: root.details.error || ""
    }
}
