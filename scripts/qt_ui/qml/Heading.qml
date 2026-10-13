import QtQuick
Item {
    property string title: ""
    property string eyebrow: ""
    implicitHeight: 42
    Rectangle {anchors.bottom: parent.bottom;width: parent.width;height: 1;color: Theme.line}
    Txt {x: 12;y: 11;width: 64;height: 20;text: parent.eyebrow;color: Theme.accentStrong;font.pixelSize: 11;font.weight: Font.Bold;font.letterSpacing: .8}
    Txt {x: parent.eyebrow ? 85 : 14;y: 10;height: 22;width: parent.width-x-12;text: parent.title;font.pixelSize: 15;font.weight: Font.DemiBold}
}
