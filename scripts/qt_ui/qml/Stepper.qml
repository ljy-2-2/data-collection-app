import QtQuick
import QtQuick.Layouts
Card {
    id: control
    property var titles: []
    property int step: 1
    property bool returnVisible: false
    property bool returnEnabled: true
    signal returnClicked()
    implicitHeight: titles.length===2 ? 56 : 50
    RowLayout {
        anchors.fill: parent;anchors.leftMargin: 14;anchors.rightMargin: 14;spacing: 12
        Repeater {
            model: control.titles
            Item {
                required property string modelData
                required property int index
                Layout.fillWidth: true;Layout.fillHeight: true
                Rectangle {x: 132;y: parent.height/2;width: Math.max(0,parent.width-148);height: 1;color: index+1<control.step ? Theme.green : Theme.line;visible: index<control.titles.length-1}
                Rectangle {x: -4;anchors.verticalCenter: parent.verticalCenter;width: 36;height: 36;radius: 18;color: Theme.accentSoft;visible: control.step===index+1}
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter;width: 28;height: 28;radius: 14
                    color: control.step===index+1 ? Theme.accent : index+1<control.step ? Theme.greenSoft : Theme.surface
                    border.color: control.step===index+1 ? Theme.accent : index+1<control.step ? Theme.green : Theme.lineStrong
                    Txt {anchors.centerIn: parent;text: index+1<control.step ? "✓" : String(index+1);color: control.step===index+1 ? "white" : index+1<control.step ? Theme.green : Theme.inkSoft;font.pixelSize: 12;font.bold: true}
                }
                Txt {x: 36;anchors.verticalCenter: parent.verticalCenter;width: Math.max(0,parent.width-38);text: modelData;color: control.step===index+1 ? Theme.ink : Theme.inkMuted;font.weight: Font.DemiBold}
            }
        }
        Item {Layout.preferredWidth: control.titles.length===2 ? 108 : 0;Layout.fillHeight: true;visible: control.titles.length===2
            Btn {anchors.right: parent.right;anchors.verticalCenter: parent.verticalCenter;visible: control.returnVisible;enabled: control.returnEnabled;primary: true;text: "返回配置";glyph: "ArrowLeft";onClicked: control.returnClicked()}
        }
    }
}
