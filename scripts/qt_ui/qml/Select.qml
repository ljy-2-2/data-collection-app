import QtQuick
import QtQuick.Controls
ComboBox {
    id: control
    implicitHeight: 34
    implicitWidth: 160
    leftPadding: 9
    rightPadding: 28
    opacity: enabled ? 1 : .58
    font.family: Theme.font
    font.pixelSize: 13
    delegate: ItemDelegate {
        width: control.width
        height: 34
        contentItem: Txt {text: control.textRole ? modelData[control.textRole] : modelData; color: highlighted ? Theme.accentStrong : Theme.ink}
        highlighted: control.highlightedIndex === index
        background: Rectangle {color: highlighted ? Theme.accentSoft : Theme.surface}
    }
    indicator: Image {x: control.width - 24;y: (control.height-height)/2;width: 15;height: 15;source: Theme.icon("ChevronDown")}
    contentItem: Txt {text: control.displayText; color: Theme.ink; font.pixelSize: control.font.pixelSize}
    background: Rectangle {color: Theme.dark ? "#303030" : Theme.surface;radius: 7;border.color: control.activeFocus ? Theme.accentStrong : Theme.lineStrong}
    popup: Popup {
        y: control.height+3;width: control.width;padding: 4
        implicitHeight: Math.min(contentItem.implicitHeight+8,260)
        contentItem: ListView {clip: true;implicitHeight: contentHeight;model: control.popup.visible ? control.delegateModel : null;currentIndex: control.highlightedIndex;ScrollIndicator.vertical: ScrollIndicator {}}
        background: Rectangle {color: Theme.surface;radius: 7;border.color: Theme.lineStrong}
    }
}
