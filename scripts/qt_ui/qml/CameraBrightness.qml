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
    property bool active: true
    property bool available: false
    property bool editable: true
    property var details: scope === "tianji" ? ((backend.teleopCameraSettings[cameraKey] || {}).brightness || {}) : (backend.umiCameraBrightness[cameraKey] || {})
    property bool adjustable: available && editable && !!details.supported && !details.busy && !details.error
    property real draftValue: 0
    readonly property real lower: details.supported ? details.min : 0
    readonly property real upper: details.supported ? details.max : 1
    readonly property real increment: details.supported ? details.step : 1
    implicitHeight: compact ? (sidePanel ? 50 : 28)+(note.text ? 16 : 0) : sidePanel ? 79 : 55
    implicitWidth: 230

    function refresh() {
        debounce.stop()
        if (available && cameraKey && (scope !== "tianji" || editable)) {
            if (scope === "tianji") backend.loadTeleopCameraSettings(cameraKey)
            else backend.loadCameraBrightness(cameraKey)
        }
    }
    function syncValue() {
        if (details && slider && number && details.supported && !details.busy && !slider.pressed && !number.editing) {
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

    Txt {visible: root.sidePanel;x: 8;y: 5;text: "亮度";font.pixelSize: root.compact ? 10 : 11;color: Theme.inkMuted}
    RowLayout {x: 8;y: root.sidePanel ? (root.compact ? 22 : 26) : 4;width: parent.width-16;height: root.compact ? 22 : 28;spacing: 4
        Txt {visible: !root.sidePanel;text: "亮度";font.pixelSize: root.compact ? 10 : 11;color: Theme.inkMuted}
        Slider {
            id: slider;objectName: "cameraBrightnessSlider"
            Layout.fillWidth: true;Layout.minimumWidth: 38;Layout.preferredHeight: root.compact ? 22 : 28
            from: root.lower;to: root.upper;stepSize: root.increment
            value: Math.max(root.lower, Math.min(root.upper, root.draftValue))
            enabled: root.adjustable;live: true;snapMode: Slider.SnapAlways
            focusPolicy: Qt.StrongFocus;Accessible.name: root.cameraName+"亮度"
            opacity: enabled?1:.45
            onMoved: {root.draftValue=value;number.text=String(value);if (!pressed) debounce.restart()}
            onPressedChanged: {if (pressed) debounce.stop();else root.commit(value)}
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
            Layout.preferredWidth: root.compact ? 36 : 43;Layout.preferredHeight: root.compact ? 22 : 27;padding: 3;font.pixelSize: root.compact ? 10 : 11
            text: root.details.supported?String(root.draftValue):"—";enabled: root.adjustable
            horizontalAlignment: Text.AlignHCenter;Accessible.name: root.cameraName+"亮度数值"
            validator: IntValidator {bottom: root.lower;top: root.upper}
            onTextEdited: editing=true
            onEditingFinished: {var next=Number(text);var valid=acceptableInput;editing=false;if (valid) root.commit(next);else root.syncValue()}
        }
        Btn {
            objectName: "cameraBrightnessDefault";Layout.preferredWidth: root.compact ? 32 : 38;Layout.preferredHeight: root.compact ? 22 : 27;textSize: root.compact ? 9 : 10
            text: root.details.error?"重试":root.details.supported?"默认":"刷新"
            enabled: root.available && root.editable && !root.details.busy
            Accessible.name: root.cameraName+(root.details.supported&&!root.details.error?"恢复默认亮度":"刷新亮度")
            onClicked: {if (root.details.error || !root.details.supported) root.refresh();else root.commit(root.details.default)}
        }
    }
    Txt {
        id: note;visible: !!text;x: 8;y: root.compact ? (root.sidePanel ? 48 : 28) : root.sidePanel ? 59 : 35;width: parent.width-16;height: 15;font.pixelSize: 10
        text: root.compact ? (root.details.error || "") : !root.active?"未启用":root.details.busy?"正在读取 / 应用亮度…":root.details.error?root.details.error:!root.available?"等待相机画面":root.details.supported?"范围 "+root.lower+" ～ "+root.upper:root.details.loaded?root.details.message||"相机不支持亮度调节":"正在读取亮度…"
        color: root.details.error?Theme.red:Theme.inkSoft
        HoverHandler {id: hover}
        ToolTip.visible: !root.compact && hover.hovered && !!(root.details.error || root.details.message)
        ToolTip.text: root.details.error || root.details.message || ""
    }
}
