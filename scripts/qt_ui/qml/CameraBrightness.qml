import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Item {
    id: root
    property string cameraKey: ""
    property string cameraName: "相机"
    property string scope: "umi"
    property bool active: true
    property bool available: false
    property bool editable: true
    property var details: scope === "tianji" ? ((backend.teleopCameraSettings[cameraKey] || {}).brightness || {}) : (backend.umiCameraBrightness[cameraKey] || {})
    property bool adjustable: available && editable && !!details.supported && !details.busy && !details.error
    property real draftValue: 0
    readonly property real lower: details.supported ? details.min : 0
    readonly property real upper: details.supported ? details.max : 1
    readonly property real increment: details.supported ? details.step : 1
    implicitHeight: 55
    implicitWidth: 230

    function refresh() {
        debounce.stop()
        if (available && cameraKey && (scope !== "tianji" || editable)) {
            if (scope === "tianji") backend.loadTeleopCameraSettings(cameraKey)
            else backend.loadCameraBrightness(cameraKey)
        }
    }
    function syncValue() {
        if (details.supported && !details.busy && !slider.pressed && !number.editing) {
            draftValue = details.value
            number.text = String(draftValue)
        }
    }
    function commit(value) {
        debounce.stop()
        if (!adjustable || !isFinite(value)) return
        var index = Math.max(0, Math.min(Math.floor((upper-lower)/increment), Math.round((value-lower)/increment)))
        var snapped = lower + index*increment
        draftValue = snapped
        number.text = String(snapped)
        if (snapped !== details.value) {
            if (scope === "tianji") backend.setTeleopCameraBrightness(cameraKey, snapped)
            else backend.setCameraBrightness(cameraKey, snapped)
        }
    }
    onCameraKeyChanged: { syncValue(); refresh() }
    onAvailableChanged: {if (scope !== "tianji" || !details.loaded) refresh()}
    onEditableChanged: {if (editable && !details.loaded && !details.busy && !details.error) refresh()}
    function refreshIfNeeded() {if (available && !details.loaded && !details.busy && !details.error) refresh()}
    onDetailsChanged: {syncValue();Qt.callLater(refreshIfNeeded)}
    Component.onCompleted: { syncValue(); refresh() }
    Timer {id: debounce;interval: 200;onTriggered: root.commit(root.draftValue)}

    RowLayout {x: 8;y: 4;width: parent.width-16;height: 28;spacing: 5
        Txt {text: "亮度";font.pixelSize: 11;color: Theme.inkMuted}
        Slider {
            id: slider;objectName: "cameraBrightnessSlider"
            Layout.fillWidth: true;Layout.minimumWidth: 38;height: 28
            from: root.lower;to: root.upper;stepSize: root.increment
            value: Math.max(root.lower, Math.min(root.upper, root.draftValue))
            enabled: root.adjustable;live: true;snapMode: Slider.SnapAlways
            focusPolicy: Qt.StrongFocus;Accessible.name: root.cameraName+"亮度"
            opacity: enabled?1:.45
            onMoved: {root.draftValue=value;if (!pressed) debounce.restart()}
            onPressedChanged: {if (!pressed) root.commit(value)}
            background: Rectangle {
                x: slider.leftPadding;y: slider.topPadding+slider.availableHeight/2-height/2
                width: slider.availableWidth;height: 4;radius: 2;color: Theme.lineStrong
                Rectangle {width: slider.visualPosition*parent.width;height: parent.height;radius: 2;color: Theme.accent}
            }
            handle: Rectangle {
                x: slider.leftPadding+slider.visualPosition*(slider.availableWidth-width)
                y: slider.topPadding+slider.availableHeight/2-height/2
                width: 13;height: 13;radius: 7;color: Theme.accent
                border.width: slider.activeFocus?2:1;border.color: slider.activeFocus?Theme.accentStrong:Theme.surface
            }
        }
        Field {
            id: number;objectName: "cameraBrightnessNumber"
            property bool editing: false
            Layout.preferredWidth: 43;height: 27;padding: 4;font.pixelSize: 11
            text: root.details.supported?String(root.draftValue):"—";enabled: root.adjustable
            horizontalAlignment: Text.AlignHCenter;Accessible.name: root.cameraName+"亮度数值"
            validator: IntValidator {bottom: root.lower;top: root.upper}
            onTextEdited: editing=true
            onEditingFinished: {var next=Number(text);var valid=acceptableInput;editing=false;if (valid) root.commit(next);else root.syncValue()}
        }
        Btn {
            objectName: "cameraBrightnessDefault";Layout.preferredWidth: 38;height: 27;textSize: 10
            text: root.details.error?"重试":root.details.supported?"默认":"刷新"
            enabled: root.available && root.editable && !root.details.busy
            Accessible.name: root.cameraName+(root.details.supported&&!root.details.error?"恢复默认亮度":"刷新亮度")
            onClicked: {if (root.details.error || !root.details.supported) root.refresh();else root.commit(root.details.default)}
        }
    }
    Txt {
        id: note;x: 8;y: 35;width: parent.width-16;height: 15;font.pixelSize: 10
        text: !root.active?"未启用":root.details.busy?"正在读取 / 应用亮度…":root.details.error?root.details.error:root.scope==="tianji"&&!root.editable?"遥操及采集期间锁定设置":!root.available?"等待相机画面":root.details.supported?(root.scope==="tianji"?"硬件范围 ":"范围 ")+root.lower+" ～ "+root.upper:root.details.loaded?root.details.message||"相机不支持亮度调节":"正在读取亮度…"
        color: root.details.error?Theme.red:Theme.inkSoft
        HoverHandler {id: hover}
        ToolTip.visible: hover.hovered && !!(root.details.error || root.details.message)
        ToolTip.text: root.details.error || root.details.message || ""
    }
}
