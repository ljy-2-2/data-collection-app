import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Collection.Native 1.0
ApplicationWindow {
    id: app
    objectName: "mainWindow"
    width: 1280;height: 800;minimumWidth: 360;minimumHeight: 300
    visible: true
    title: "小凯实验室数采平台"
    flags: Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowMinimizeButtonHint | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
    color: Theme.bg
    font.family: Theme.font;font.pixelSize: 13
    property var s: backend.state
    readonly property string pageError: {
        let messages=[s.error]
        if(s.page==="teleop" && s.teleopStep===2){
            let status=s.robot==="kuavo-5w"?s.kuavo:s.robot==="tianji"?s.tianji:s.g2
            let collection=status.collection||{}
            if(s.robot!=="tianji" || s.tianjiStatusFresh)
                messages.push(status.error,status.camera_error,collection.error,(collection.upload||{}).error)
            if(s.robot==="tianji")messages.push(s.cameraApplyError)
        }
        if(s.page==="umi")messages.push(s.umi.error)
        return messages.filter((message,index,list)=>!!message && list.indexOf(message)===index).join("\n")
    }
    property bool narrow: width<780
    // The desktop window manager owns the single native title bar.
    property bool recordKeyBlocked: usageGuide.previewVisible || umiPanel.recordDialogOpen
    onClosing: close=>{close.accepted=s.closing;if(!close.accepted)backend.closeRequested()}
    Backdrop {anchors.fill: parent;dark: Theme.dark;login: !s.authenticated}
    Startup {objectName: "startupPage";anchors.fill: parent;visible: !s.ready}
    Login {objectName: "loginPage";anchors.fill: parent;visible: s.ready && !s.authenticated}
    Item {
        objectName: "collectionPage"
        anchors.fill: parent;anchors.leftMargin: app.narrow?10:14;anchors.rightMargin: app.narrow?10:14;anchors.topMargin: 10;anchors.bottomMargin: 10
        visible: s.ready && s.authenticated
        Card {
            id: header;width: parent.width;height: app.width<480?98:58;radius: 13
            Row {x: 12;y: 12;spacing: 8
                Logo {width: 32;height: 32;source: backend.assetRoot+(Theme.dark?"K_dark.png":"K.png")}
                Column {spacing: 1
                    Txt {objectName: "headerEnglishTitle";text: "G-Lab Data Collection";font.pixelSize: 9;font.weight: Font.Bold;color: Theme.accentStrong;height: 13}
                    Txt {objectName: "headerChineseTitle";text: "小凯实验室数据采集";font.pixelSize: 14;font.weight: Font.Bold;height: 18}
                }
            }
            Rectangle {
                anchors.right: parent.right;anchors.rightMargin: 12;y: app.width<480?58:14;width: themeRow.width+6;height: 30;radius: 9;color: Theme.muted;border.color: Theme.line
                Row {id: themeRow;x: 3;y: 3;spacing: 2
                    Repeater {model: ["跟随系统","浅色","深色"]
                        Button {required property string modelData;required property int index;property string mode: ["system","light","dark"][index];text: modelData;width: index===0?68:44;height: 24;hoverEnabled: true
                            background: Rectangle {radius: 6;color: s.theme===parent.mode || parent.hovered ? Theme.surface : "transparent"}
                            contentItem: Txt {selectable: false;text: parent.text;font.pixelSize: 12;font.weight: Font.Bold;color: s.theme===parent.mode?Theme.accentStrong:Theme.ink;horizontalAlignment: Text.AlignHCenter}
                            onClicked: backend.setValue("theme",mode)
                        }
                    }
                }
            }
        }
        Card {
            id: sidebar;x: 0;y: header.height+12;width: app.narrow?parent.width:148;height: app.narrow?48:parent.height-y;radius: 10
            Grid {x: 6;y: 6;width: parent.width-12;columns: app.narrow?3:1;spacing: 2
                Repeater {model: backend.umiOnly ? ["UMI 采集"] : ["遥操作采集","UMI 采集","Ego 采集"]
                    Button {id: navButton;required property string modelData;required property int index;property string page: backend.umiOnly ? "umi" : ["teleop","umi","ego"][index];width: app.narrow?(sidebar.width-16)/(backend.umiOnly?1:3):sidebar.width-12;height: 34;hoverEnabled: true
                        objectName: "nav-"+page
                        padding: 0;leftPadding: 9;rightPadding: 9
                        background: Rectangle {radius: 7;color: s.page===parent.page?Theme.accentSoft:parent.hovered?Theme.muted:"transparent";border.color: s.page===parent.page?"#30f4c520":"transparent"}
                        contentItem: Item {
                            Image {id: navIcon;objectName: "navIcon-"+navButton.page;anchors.left: parent.left;anchors.verticalCenter: parent.verticalCenter;width: 16;height: 16;source: Theme.icon(backend.umiOnly ? "Camera" : ["Gamepad2","Camera","Glasses"][navButton.index],s.page===navButton.page?"accent":"")}
                            Txt {selectable: false;objectName: "navText-"+navButton.page;anchors.left: navIcon.right;anchors.leftMargin: 7;anchors.right: parent.right;anchors.verticalCenter: parent.verticalCenter;text: navButton.modelData;font.pixelSize: 13;color: s.page===navButton.page?Theme.accentStrong:Theme.inkMuted;font.weight: s.page===navButton.page?Font.Medium:Font.Normal;wrapMode: Text.NoWrap}
                        }
                        enabled: !s.busy && !s.closing
                        onClicked: backend.navigate(page)
                    }
                }
            }
            Rectangle {visible: !app.narrow;x: 8;y: 118;width: parent.width-16;height: 1;color: Theme.line}
            Button {objectName: "platformLink";visible: !app.narrow;x: 6;y: 125;width: parent.width-12;height: 53;hoverEnabled: true
                background: Rectangle {radius: 7;color: parent.hovered?Theme.muted:"transparent"}
                contentItem: Item {
                    Image {objectName: "platformLinkIcon";x: 9;anchors.verticalCenter: parent.verticalCenter;width: 16;height: 16;source: Theme.icon("Database")}
                    Column {x: 32;anchors.verticalCenter: parent.verticalCenter;spacing: 4
                        Txt {selectable: false;text: "跳转平台";font.pixelSize: 13;color: Theme.inkMuted}
                        Txt {selectable: false;text: backend.platformLocation;font.pixelSize: 11;color: Theme.inkSoft}
                    }
                    Image {objectName: "platformExternalIcon";anchors.right: parent.right;anchors.rightMargin: 7;anchors.verticalCenter: parent.verticalCenter;width: 13;height: 13;source: Theme.icon("ExternalLink")}
                }
                onClicked: backend.openPlatform()
            }
            Rectangle {visible: !app.narrow;x: 6;anchors.bottom: parent.bottom;anchors.bottomMargin: 6;width: parent.width-12;height: 50;radius: 7;color: Theme.muted;border.color: Theme.line
                Rectangle {x: 7;y: 11;width: 28;height: 28;radius: 7;color: Theme.accent
                    Txt {anchors.centerIn: parent;text: s.username.slice(0,1).toUpperCase();color: "#2b2b2b";font.pixelSize: 12;font.bold: true}
                }
                Column {x: 42;y: 9;width: parent.width-73;spacing: 0
                    Txt {width: parent.width;text: s.username;font.pixelSize: 12;font.weight: Font.Bold;height: 17}
                    Txt {text: "v2.0.0";font.pixelSize: 11;color: Theme.inkSoft;height: 16}
                }
                Btn {anchors.right: parent.right;anchors.rightMargin: 5;y: 11;width: 28;height: 28;glyph: "LogOut";ghost: true;enabled: !s.busy;onClicked: backend.logout()}
            }
        }
        WheelScroll {
            id: scroll;objectName: "pageScroll"
            x: app.narrow?0:sidebar.width+12;y: sidebar.y+(app.narrow?sidebar.height+12:0)
            width: parent.width-x;height: parent.height-y
            contentWidth: width;contentHeight: page.height;clip: true;boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {policy: ScrollBar.AsNeeded;width: 8;interactive: false}
            Column {
                id: page;width: scroll.width;spacing: 12
                Item {objectName: "pageTools";width: parent.width;height: visible?30:0;visible:s.page==="umi"||(s.page==="teleop"&&s.teleopStep===2)||!!pageNotice.message
                    Rectangle {
                        id: pageNotice;objectName: "pageNotice"
                        readonly property string message: [app.pageError,s.notice].filter(Boolean).join("\n")
                        anchors.left: parent.left;anchors.right: usageGuide.visible?usageGuide.left:parent.right;anchors.rightMargin: usageGuide.visible?12:0
                        height: parent.height;visible: !!message;radius: 7
                        color: app.pageError?Theme.redSoft:Theme.muted
                        border.color: Theme.line
                        Txt {objectName: "pageNoticeText";anchors.fill: parent;anchors.leftMargin: 10;anchors.rightMargin: 36;text: pageNotice.message;font.pixelSize: 12;color: app.pageError?Theme.red:Theme.inkMuted;wrapMode: Text.NoWrap}
                        Btn {objectName: "pageNoticeDismiss";visible: !!s.error;anchors.right: parent.right;anchors.rightMargin: 3;anchors.verticalCenter: parent.verticalCenter;glyph: "X";ghost: true;width: 24;height: 24;Accessible.name: "关闭错误提示";onClicked: backend.clearError()}
                    }
                    UsageGuide {id: usageGuide;objectName: "usageGuide";visible: s.page==="umi" || (s.page==="teleop" && s.teleopStep===2);anchors.right: parent.right;anchors.verticalCenter: parent.verticalCenter;viewportWidth: app.width;viewportHeight: app.height;source: s.page==="teleop"&&s.robot==="kuavo-5w"?"":backend.assetRoot+(s.page==="umi"?"umi-usage-guide.png":s.robot==="tianji"?"tianji-usage-guide.png":"teleop-usage-guide.png")}
                }
                Stepper {objectName: "pageStepper";visible: s.page!=="ego";width: parent.width;titles: s.page==="umi"?["选择配置","检查硬件","采集监控"]:["选择配置","遥操作采集"];step: s.page==="umi"?s.umiStep:s.teleopStep;returnVisible: s.page==="teleop"&&s.teleopStep===2;returnEnabled: s.robot==="kuavo-5w" ? !kuavoTeleop.locked : !teleop.locked;onReturnClicked: backend.returnTeleop()}
                Setup {width: parent.width;visible: s.page==="teleop"&&s.teleopStep===1}
                Teleop {id: teleop;objectName: "teleopWorkspace";width: parent.width;visible: s.page==="teleop"&&s.teleopStep===2&&s.robot!=="kuavo-5w"}
                Kuavo {id: kuavoTeleop;objectName: "kuavoWorkspace";width: parent.width;visible: s.page==="teleop"&&s.teleopStep===2&&s.robot==="kuavo-5w"}
                Umi {id: umiPanel;width: parent.width;visible: s.page==="umi"}
                Card {visible: s.page==="ego";width: parent.width;height: 580}
                Btn {visible: app.narrow;text: "退出登录";glyph: "LogOut";ghost: true;enabled: !s.busy;onClicked: backend.logout()}
            }
        }
        Connections {target: backend;property string lastPage: "teleop";function onChanged(){if(lastPage!==backend.state.page){scroll.contentY=0;lastPage=backend.state.page}}}
    }
    Rectangle {anchors.fill: parent;visible: s.closing;color: "#99000000";z: 100
        Card {anchors.centerIn: parent;width: Math.min(420,parent.width-32);height: 120
            Txt {anchors.fill: parent;anchors.margins: 20;text: s.notice;wrapMode: Text.Wrap;elide: Text.ElideNone;horizontalAlignment: Text.AlignHCenter}
        }
        MouseArea {anchors.fill: parent}
    }
}
