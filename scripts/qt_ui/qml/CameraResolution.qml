import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Item {
    id: root
    property string cameraKey: ""
    property string cameraName: "相机"
    property string scope: "umi"
    property bool available: false
    property bool editable: true
    property var details: scope === "tianji" ? ((backend.teleopCameraSettings[cameraKey] || {}).resolution || {}) : (backend.umiCameraResolution[cameraKey] || {})
    property var choices: details.options || []
    implicitHeight: 58
    function refresh() { if (available && cameraKey && (scope !== "tianji" || editable)) {if (scope === "tianji") backend.loadTeleopCameraSettings(cameraKey);else backend.loadCameraResolution(cameraKey)} }
    onCameraKeyChanged: refresh()
    onAvailableChanged: {if (scope !== "tianji" || !details.loaded) refresh()}
    onEditableChanged: {if (editable && !details.loaded && !details.busy && !details.error) refresh()}
    function refreshIfNeeded() {if (available && !details.selected && !details.busy && !details.error) refresh()}
    onDetailsChanged: Qt.callLater(refreshIfNeeded)
    Component.onCompleted: refresh()
    RowLayout {x: 8;y: 4;width: parent.width-16;height: 29;spacing: 5
        Txt {text: "录制分辨率";font.pixelSize: 11;color: Theme.inkMuted}
        Select {
            objectName: "cameraResolutionSelect";Layout.fillWidth: true;height: 29;font.pixelSize: 11
            model: root.choices.map(m=>m.label)
            currentIndex: root.details.selected ? root.choices.findIndex(m=>m.id===root.details.selected.id) : -1
            displayText: root.details.selected ? root.details.selected.label : "请选择分辨率"
            enabled: root.available && root.editable && !!root.details.editable && !root.details.busy && root.choices.length>0
            Accessible.name: root.cameraName+"采集分辨率"
            ToolTip.visible: hovered
            ToolTip.text: displayText
            onActivated: {if (root.scope === "tianji") backend.setTeleopCameraResolution(root.cameraKey,root.choices[currentIndex].id);else backend.setCameraResolution(root.cameraKey,root.choices[currentIndex].id)}
        }
        Btn {text: "刷新";textSize: 10;Layout.preferredWidth: 38;height: 27
            enabled: root.available && (root.scope!=="tianji"||root.editable) && !root.details.busy;onClicked: root.refresh()}
    }
    Txt {x: 8;y: 36;width: parent.width-16;height: 16;font.pixelSize: 10
        text: root.details.busy?"正在读取 / 验证实际画面…":root.details.error?root.details.error:!root.available?"等待相机连接":root.editable?(root.scope==="tianji"?"按所选分辨率录制 · 验证实际画面":"按所选分辨率录制 · 采集前验证帧率"):root.scope==="tianji"?"遥操及采集期间锁定设置":"采集期间锁定；返回检查硬件可修改"
        color: root.details.error?Theme.red:Theme.inkSoft
        HoverHandler {id: hover}
        ToolTip.visible: hover.hovered && !!root.details.error
        ToolTip.text: root.details.error || ""
    }
}
