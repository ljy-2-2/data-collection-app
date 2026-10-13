import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects
Item {
    id: root
    property bool savedPassword: false
    function submit() {backend.login(username.text,password.text,remember.checked,autoLogin.checked,savedPassword)}
    Connections {
        target: backend
        function onChanged() {
            let p=backend.state.preferences;
            if(p.username && !username.activeFocus && !password.activeFocus){username.text=p.username;root.savedPassword=!!p.has_saved_password;remember.checked=!!p.remember_password;autoLogin.checked=!!p.auto_login}
        }
        function onResult(action,ok) {if(action==="login" && ok){password.text="";root.savedPassword=false}}
    }
    WheelScroll {
        anchors.fill: parent;contentHeight: Math.max(height,card.height+48);clip: true;boundsBehavior: Flickable.StopAtBounds
        Card {
            id: card
            width: Math.min(parent.width-48,380);height: content.implicitHeight+56
            x: (parent.width-width)/2;y: Math.max(24,(root.height-height)/2)
            radius: 14
            layer.effect: MultiEffect {shadowEnabled: true;shadowBlur: .8;shadowVerticalOffset: 18;shadowColor: Theme.dark ? "#3d000000" : "#24201826"}
            ColumnLayout {
                id: content
                anchors.left: parent.left;anchors.right: parent.right;anchors.top: parent.top;anchors.margins: 28
                spacing: 16
                RowLayout {
                    Layout.fillWidth: true;Layout.bottomMargin: 2;spacing: 11
                    Logo {source: backend.assetRoot+"K.png";Layout.preferredWidth: 42;Layout.preferredHeight: 42}
                    Column {spacing: 2
                        Txt {text: "ROBOT DATA COLLECTION";color: Theme.accentStrong;font.pixelSize: 11;font.bold: true;font.letterSpacing: .66;height: 16}
                        Txt {text: "登录";font.pixelSize: 23;height: 29}
                    }
                }
                ColumnLayout {Layout.fillWidth: true;spacing: 6
                    Txt {text: "用户名";color: Theme.inkMuted;font.weight: Font.DemiBold;Layout.preferredHeight: 19}
                    Field {id: username;objectName: "loginUsername";Layout.fillWidth: true;implicitHeight: 38;enabled: backend.state.ready && !backend.state.busy;onTextEdited: root.savedPassword=false;onAccepted: password.forceActiveFocus()}
                }
                ColumnLayout {Layout.fillWidth: true;spacing: 6
                    Txt {text: "密码";color: Theme.inkMuted;font.weight: Font.DemiBold;Layout.preferredHeight: 19}
                    Field {id: password;objectName: "loginPassword";Layout.fillWidth: true;implicitHeight: 38;echoMode: TextInput.Password;enabled: backend.state.ready && !backend.state.busy;placeholderText: root.savedPassword ? "••••••••" : "";onTextEdited: root.savedPassword=false;onAccepted: root.submit()}
                }
                RowLayout {Layout.fillWidth: true;spacing: 8
                    Tick {id: remember;text: "记住账号密码";enabled: !backend.state.busy;onClicked: if(!checked) autoLogin.checked=false}
                    Item {Layout.fillWidth: true}
                    Tick {id: autoLogin;text: "自动登录";enabled: !backend.state.busy;onClicked: if(checked) remember.checked=true}
                }
                Rectangle {visible: backend.state.error!=="";Layout.fillWidth: true;implicitHeight: errorText.implicitHeight+18;color: Theme.redSoft;border.color: Theme.line;radius: 8
                    Txt {id: errorText;anchors.left: parent.left;anchors.right: parent.right;anchors.margins: 10;y: 9;text: backend.state.error;color: Theme.red;font.pixelSize: 12;wrapMode: Text.Wrap;elide: Text.ElideNone}
                }
                Btn {objectName: "loginSubmit";Layout.fillWidth: true;text: backend.state.busy==="login" ? "正在登录" : "登录";primary: true;busy: backend.state.busy==="login";enabled: backend.state.ready && !backend.state.busy;onClicked: root.submit()}
            }
        }
    }
}
