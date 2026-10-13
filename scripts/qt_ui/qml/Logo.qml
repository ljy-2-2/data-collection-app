import QtQuick
import QtQuick.Effects
Item {
    id: root
    property url source
    property real radius: 10
    Image {anchors.fill: parent;source: root.source;mipmap: true;layer.enabled: true
        layer.effect: MultiEffect {maskEnabled: true;maskSource: mask;maskThresholdMin: .5;maskSpreadAtMin: 1}
    }
    Item {id: mask;anchors.fill: parent;visible: false;layer.enabled: true
        Rectangle {anchors.fill: parent;radius: root.radius;color: "white"}
    }
    Rectangle {anchors.fill: parent;color: "transparent";radius: root.radius;border.color: Theme.line}
}
