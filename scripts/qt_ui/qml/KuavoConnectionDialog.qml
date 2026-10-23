import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Popup {
    id:root;objectName:"kuavoConnectionDialog"
    property var connection: ({})
    property bool saving:false
    property bool editable:true
    property string error:""
    parent:Overlay.overlay;anchors.centerIn:parent;modal:true;padding:20
    width:Math.min(580,parent?parent.width-32:580)
    height:Math.min(parent?parent.height-48:700,body.implicitHeight+40)
    closePolicy:saving?Popup.NoAutoClose:Popup.CloseOnEscape
    background:Rectangle {color:Theme.surface;radius:12;border.color:Theme.line}
    Overlay.modal:Rectangle {color:"#52000000"}
    onOpened:{host.text=connection.host||"192.168.26.1";port.text=String(connection.port||7443);certificate.text=connection.certificate_sha256||"";token.text="";error=""}
    onClosed:token.text=""
    Connections {target:backend
        function onKuavoSettingsSaved(ok,message) {if(!root.saving)return;root.saving=false;if(ok)root.close();else root.error=message}
    }
    contentItem:WheelScroll {
        contentWidth:width;contentHeight:body.implicitHeight;clip:true
        ScrollBar.vertical:ScrollBar {interactive:false}
        ColumnLayout {id:body;width:parent.width;spacing:10
            Txt {text:"机器人连接";font.pixelSize:16;font.weight:Font.DemiBold}
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:"本电脑通过自身网络连接机器人。新电脑首次填写配置，并准备 PICO 接收服务和 SDK；保存后点击检查设备。";font.pixelSize:12;color:Theme.inkMuted}
            Txt {text:"机器人服务 IP"}
            TextField {id:host;objectName:"kuavoConnectionHost";Layout.fillWidth:true;enabled:!root.saving;selectByMouse:true}
            Txt {text:"端口"}
            TextField {id:port;Layout.fillWidth:true;enabled:!root.saving;validator:IntValidator {bottom:1;top:65535}}
            Txt {text:"证书 SHA256 指纹（来自机器人连接配置）";Layout.fillWidth:true;wrapMode:Text.Wrap}
            TextField {id:certificate;Layout.fillWidth:true;enabled:!root.saving;selectByMouse:true;maximumLength:64}
            Txt {text:"连接密钥（来自配套 token 文件，每次保存需填写）";Layout.fillWidth:true;wrapMode:Text.Wrap}
            TextField {id:token;objectName:"kuavoConnectionToken";Layout.fillWidth:true;enabled:!root.saving;echoMode:TextInput.Password;maximumLength:64;selectByMouse:true}
            Txt {visible:!!root.error;Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.error;color:Theme.red}
            RowLayout {Layout.alignment:Qt.AlignRight
                Btn {text:"取消";enabled:!root.saving;onClicked:root.close()}
                Btn {text:root.saving?"保存中…":"保存";primary:true;enabled:root.editable&&!root.saving;onClicked:{root.saving=true;root.error="";backend.saveKuavoSettings("connection",{host:host.text,port:Number(port.text),certificate_sha256:certificate.text,token:token.text})}}
            }
        }
    }
}
