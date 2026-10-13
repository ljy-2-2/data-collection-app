import QtQuick
import QtQuick.Effects
Rectangle {
    color: Theme.surface
    border.color: Theme.line
    radius: 11
    layer.enabled: true
    layer.effect: MultiEffect {shadowEnabled: true; shadowColor: Theme.dark ? "#24000000" : "#10181826"; shadowBlur: 0.2; shadowVerticalOffset: 1}
}
