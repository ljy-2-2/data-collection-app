import QtQuick
import QtQuick.Controls
TextEdit {
    id: root
    property bool selectable: true
    // Existing callers specify Text elision. Read-only text scrolls while
    // selecting instead, so clipped diagnostics can still be copied in full.
    property int elide: Text.ElideRight
    color: Theme.ink
    font.family: Theme.font
    font.pixelSize: 13
    renderType: TextEdit.NativeRendering
    verticalAlignment: TextEdit.AlignVCenter
    textFormat: TextEdit.PlainText
    readOnly: true
    selectByMouse: selectable
    activeFocusOnPress: selectable
    enabled: selectable
    cursorVisible: false
    clip: true
    selectionColor: Theme.accentSoft
    selectedTextColor: Theme.ink
    persistentSelection: false
    Accessible.role: Accessible.StaticText
    Accessible.name: text
    TapHandler {
        acceptedButtons: Qt.RightButton
        onTapped: {
            if (copyMenuLoader.item) copyMenuLoader.item.popup()
            else copyMenuLoader.active = true
        }
    }
    Loader {
        id: copyMenuLoader
        active: false
        onLoaded: item.popup()
        sourceComponent: Component {
            Menu {
                onClosed: Qt.callLater(() => copyMenuLoader.active = false)
                MenuItem {text: "复制所选文字";enabled: root.selectedText.length>0;onTriggered: root.copy()}
                MenuItem {text: "复制全部文字";onTriggered: {root.selectAll();root.copy()}}
                MenuItem {text: "全选";onTriggered: {root.forceActiveFocus();root.selectAll()}}
            }
        }
    }
}
