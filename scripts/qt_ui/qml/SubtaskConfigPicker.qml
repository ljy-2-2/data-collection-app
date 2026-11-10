import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ColumnLayout {
    id: root
    spacing: 4
    Component.onCompleted: backend.loadSubtaskConfigs()
    Txt {text: "子任务标注配置";font.pixelSize: 12;color: Theme.inkMuted;font.weight: Font.DemiBold;Layout.preferredHeight:18}
    RowLayout {
        Layout.fillWidth: true;spacing: 7
        Select {
            id: select;objectName: "subtaskConfigSelect";Layout.fillWidth: true;Layout.minimumWidth:0;Layout.preferredWidth:160
            contentItem: Text {text:select.displayText;color:Theme.ink;font.family:Theme.font;font.pixelSize:select.font.pixelSize;verticalAlignment:Text.AlignVCenter;elide:Text.ElideRight}
            model: [{id:"",name:"不使用子任务标注"}].concat(backend.state.subtaskConfigs || [])
            textRole: "name"
            currentIndex: Math.max(0, model.findIndex(x=>x.id===backend.state.subtaskConfigId))
            onActivated: index=>backend.selectSubtaskConfig(model[index].id)
            delegate: ItemDelegate {
                id: option
                objectName: "subtaskConfigOption"+index
                required property var modelData
                required property int index
                width: select.popup.availableWidth;height: 36
                leftPadding:6;rightPadding:6;topPadding:1;bottomPadding:1
                highlighted: select.highlightedIndex===index
                hoverEnabled:true
                background: Rectangle {
                    objectName:"subtaskConfigOptionBackground"+option.index
                    radius:4
                    color:option.highlighted || option.hovered ? Theme.accentSoft : Theme.surface
                }
                contentItem: RowLayout {
                    Txt {objectName:"subtaskConfigOptionLabel"+option.index;text:option.modelData.name;Layout.fillWidth:true;Layout.preferredHeight:18;selectable:false;elide:Text.ElideRight;color:option.highlighted || option.hovered ? Theme.accentStrong : Theme.ink}
                    Btn {visible: modelData.id!=="";text:"删除";enabled:!backend.state.subtaskConfigBusy;onClicked:backend.deleteSubtaskConfig(modelData.id)}
                }
                onClicked: {backend.selectSubtaskConfig(modelData.id);select.popup.close()}
            }
        }
        Btn {objectName:"newSubtaskConfig";text:"新建配置";onClicked:editor.open()}
    }
    Txt {Layout.fillWidth:true;visible:!!backend.state.subtaskConfig;text:backend.state.subtaskConfig ? backend.state.subtaskConfig.labels.join(" → ")+" · X 标注，Y 重置；A 开始，B 保存" : "";wrapMode:Text.Wrap;font.pixelSize:11;color:Theme.inkMuted}
    Txt {visible:!!backend.state.subtaskConfigError;text:backend.state.subtaskConfigError||"";wrapMode:Text.Wrap;Layout.fillWidth:true;color:Theme.red;font.pixelSize:12}
    Btn {visible:!!backend.state.subtaskConfigError;text:"重新加载配置";enabled:backend.state.ready;onClicked:backend.loadSubtaskConfigs()}
    Dialog {
        id:editor;objectName:"subtaskConfigDialog"
        parent:Overlay.overlay;anchors.centerIn:parent;width:Math.min(620,parent?parent.width-32:620)
        height:Math.min(620,parent?parent.height-40:620);modal:true;padding:20
        closePolicy:backend.state.subtaskConfigBusy?Popup.NoAutoClose:Popup.CloseOnEscape
        background:Rectangle {color:Theme.surface;radius:12;border.color:Theme.line}
        Overlay.modal:Rectangle {color:"#52000000"}
        onOpened:{configName.text="";cards.clear();cards.append({label:""});backend.clearSubtaskConfigError()}
        Connections {target:backend;function onSubtaskConfigSaved(){editor.close()}}
        ListModel {id:cards}
        contentItem:ColumnLayout {
            spacing:12
            Txt {text:"新建子任务标注配置";font.pixelSize:18;font.weight:Font.DemiBold}
            Txt {text:"配置名称";font.pixelSize:12}
            Field {id:configName;objectName:"subtaskConfigName";Layout.fillWidth:true;maximumLength:120;enabled:!backend.state.subtaskConfigBusy}
            ScrollView {
                id:cardScroll
                Layout.fillWidth:true;Layout.fillHeight:true;clip:true
                Column {
                    width:cardScroll.availableWidth;spacing:8
                    Repeater {
                        model:cards
                        Rectangle {
                            required property int index
                            required property string label
                            width:parent.width;height:58;radius:9;color:Theme.muted;border.color:Theme.line
                            RowLayout {
                                anchors.fill:parent;anchors.margins:8;spacing:6
                                Txt {text:index+1;font.pixelSize:12}
                                Field {objectName:"subtaskLabel"+index;Layout.fillWidth:true;text:label;placeholderText:"自定义子任务名称";maximumLength:120;enabled:!backend.state.subtaskConfigBusy;onTextEdited:cards.setProperty(index,"label",text)}
                                Btn {objectName:"subtaskUp"+index;text:"上移";enabled:index>0&&!backend.state.subtaskConfigBusy;onClicked:cards.move(index,index-1,1)}
                                Btn {objectName:"subtaskDown"+index;text:"下移";enabled:index<cards.count-1&&!backend.state.subtaskConfigBusy;onClicked:cards.move(index,index+1,1)}
                                Btn {text:"删除";enabled:cards.count>1&&!backend.state.subtaskConfigBusy;onClicked:cards.remove(index)}
                            }
                        }
                    }
                }
            }
            Btn {objectName:"addSubtaskCard";text:"添加子任务卡片";enabled:cards.count<100&&!backend.state.subtaskConfigBusy;onClicked:cards.append({label:""})}
            Txt {Layout.fillWidth:true;text:"直接编辑卡片名称即可重命名。X 按顺序循环标注，Y 让下一次 X 从第一项开始；未确认的尾段保持未标注。";wrapMode:Text.Wrap;font.pixelSize:12;color:Theme.inkMuted}
            Txt {Layout.fillWidth:true;visible:!!backend.state.subtaskConfigError;text:backend.state.subtaskConfigError||"";wrapMode:Text.Wrap;font.pixelSize:12;color:Theme.red}
            RowLayout {
                Layout.alignment:Qt.AlignRight
                Btn {objectName:"cancelSubtaskConfig";text:"取消";enabled:!backend.state.subtaskConfigBusy;onClicked:editor.close()}
                Btn {objectName:"saveSubtaskConfig";text:backend.state.subtaskConfigBusy?"保存中…":"确认保存";primary:true;enabled:configName.text.trim()!==""&&!backend.state.subtaskConfigBusy;onClicked:{var names=[];for(var i=0;i<cards.count;i++)names.push(cards.get(i).label);backend.saveSubtaskConfig({name:configName.text,labels:names})}}
            }
        }
    }
}
