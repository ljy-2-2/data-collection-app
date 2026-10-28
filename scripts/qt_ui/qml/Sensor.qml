import QtQuick
import Collection.Native 1.0
Rectangle {
    id: root
    property string side: "left"
    property bool monitoring: false
    property bool active: true
    property var s: backend.state
    property var reading: monitoring ? (backend.umiTelemetry.encoders||{})[side] || {} : s.encoders[side] || {}
    property bool online: active && !!reading.online && (!monitoring || backend.umiTelemetry.monitor_connected && reading.rate_hz>0)
    property var number: online ? (monitoring?(typeof reading.opening_angle === "number" ? reading.opening_angle : 2 * reading.position):reading.value) : null
    function degreesText(value) {
        if (typeof value !== "number" || !isFinite(value)) return "—"
        var rounded = (value * 180 / Math.PI).toFixed(2)
        return (rounded === "-0.00" ? "0.00" : rounded) + "°"
    }
    height: monitoring?158:225;color: Theme.surface;radius: 9;border.color: Theme.line;opacity: active?1:.55
    Rectangle {width: parent.width;height: 39;color: Theme.muted;radius: 9}
    Column {x: 9;y: 6;width: parent.width-65;spacing: 1
        Txt {text: root.side==="left"?"左磁编码器":"右磁编码器";font.pixelSize: 12;font.weight: Font.DemiBold;height: 16}
        Txt {width: parent.width;text: root.reading.device||root.reading.name||"";font.pixelSize: 10;color: Theme.inkSoft;height: 12}
    }
    Txt {anchors.right: parent.right;anchors.rightMargin: 9;y: 10;text: !root.active?"未启用":root.online?"在线":"离线";color: root.online?Theme.green:Theme.red;font.pixelSize: 10}
    Rectangle {x: 1;y: 39;width: parent.width-2;height: root.monitoring?90:136;color: Theme.dark?"#15202a":"#f5f6fc"
        CurveItem {anchors.fill: parent;values: root.active ? (root.monitoring ? backend.umiHistory[root.side] : root.s.encoderHistory[root.side]) : [];dark: Theme.dark;zeroBased: root.monitoring}
        Txt {anchors.centerIn: parent;visible: !root.online;text: root.active?"等待实时数据":"未启用";font.pixelSize: 11;color: Theme.inkMuted}
    }
    Rectangle {y: root.monitoring?129:175;width: parent.width;height: 1;color: Theme.line}
    Txt {x: 9;y: root.monitoring?137:183;text: root.monitoring ? "总开合角" : "原始角度";font.pixelSize: 11;color: Theme.inkSoft}
    Txt {anchors.right: parent.right;anchors.rightMargin: 9;y: root.monitoring?135:181;text: root.degreesText(root.number);font.pixelSize: 14;font.weight: Font.DemiBold}
    Tick {x: 9;y: 202;visible: !root.monitoring;text: "已闭合到最小值" + (root.s.minima[root.side]!==undefined ? "（原始零点 " + root.degreesText(root.s.minima[root.side]) + "）" : "");font.pixelSize: 11;enabled: root.active && root.online && !root.s.busy;checked: root.s.minima[root.side]!==undefined;onClicked: backend.encoderMinimum(root.side,checked)}
}
