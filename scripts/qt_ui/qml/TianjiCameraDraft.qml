import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Rectangle {
    id: root
    required property string cameraKey
    required property string cameraName
    property bool available: false
    property bool editable: false
    property var details: backend.teleopCameraSettings[cameraKey] || {}
    property var resolution: details.resolution || {}
    property var brightness: details.brightness || {}
    property var choices: resolution.options || []
    property string modeId: ""
    property string brightnessText: ""
    property bool dirty: false
    readonly property bool ready: available && editable && !details.busy
    readonly property bool valid: !dirty || (ready && (!resolution.editable || choices.some(m=>m.id===modeId)) && (!brightness.supported || (brightnessText.trim()!=="" && isFinite(Number(brightnessText)) && Number(brightnessText)>=brightness.min && Number(brightnessText)<=brightness.max && (Number(brightnessText)-brightness.min)%brightness.step===0)))
    function reset() {
        dirty=false
        modeId=(resolution.selected||{}).id || ""
        brightnessText=brightness.supported ? String(brightness.value) : ""
    }
    function changes() {
        var result={}
        if (!dirty || !ready) return result
        if (resolution.editable && modeId!==(resolution.selected||{}).id) result.mode_id=modeId
        if (brightness.supported && Number(brightnessText)!==brightness.value) result.brightness=Number(brightnessText)
        return result
    }
    onDetailsChanged: if (!dirty) reset()
    implicitHeight: body.implicitHeight+20
    radius: 9;color: Theme.muted;border.color: Theme.line
    ColumnLayout {
        id: body;x:10;y:10;width:parent.width-20;spacing:9
        Txt {text:root.cameraName;font.weight:Font.DemiBold}
        RowLayout {Layout.fillWidth:true;spacing:10
            Txt {text:"录制分辨率";Layout.preferredWidth:74;font.pixelSize:12;color:Theme.inkMuted}
            Select {objectName:"cameraDraftResolution-"+root.cameraKey;Layout.fillWidth:true;model:root.choices.map(m=>m.label);currentIndex:root.choices.findIndex(m=>m.id===root.modeId)
                displayText:currentIndex>=0?root.choices[currentIndex].label:"等待读取相机设置"
                enabled:root.ready && !!root.resolution.editable && root.choices.length>0
                onActivated:{root.dirty=true;root.modeId=root.choices[currentIndex].id}
            }
        }
        RowLayout {Layout.fillWidth:true;spacing:10
            Txt {text:"亮度";Layout.preferredWidth:74;font.pixelSize:12;color:Theme.inkMuted}
            Slider {objectName:"cameraDraftBrightnessSlider-"+root.cameraKey;Layout.fillWidth:true;enabled:root.ready && !!root.brightness.supported
                from:root.brightness.supported?root.brightness.min:0;to:root.brightness.supported?root.brightness.max:1;stepSize:root.brightness.step||1
                value:Number(root.brightnessText);snapMode:Slider.SnapAlways
                onMoved:{root.dirty=true;root.brightnessText=String(value)}
            }
            Field {objectName:"cameraDraftBrightness-"+root.cameraKey;Layout.preferredWidth:58;enabled:root.ready && !!root.brightness.supported
                text:root.brightnessText;placeholderText:"—";horizontalAlignment:Text.AlignHCenter
                validator:IntValidator {bottom:root.brightness.min||0;top:root.brightness.max||0}
                onTextEdited:{root.dirty=true;root.brightnessText=text}
            }
        }
        Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;font.pixelSize:11;color:root.details.error || !root.valid ? Theme.red:Theme.inkSoft
            text:root.details.busy?"正在读取 / 验证实际画面…":root.details.error?root.details.error:!root.available?"相机未连接":!root.valid?"请填写支持范围内的亮度，且符合步长":!root.editable?"遥操及采集期间锁定设置":root.brightness.supported?"亮度范围 "+root.brightness.min+" ～ "+root.brightness.max+" · 步长 "+root.brightness.step:root.brightness.loaded?(root.brightness.message||"相机不支持亮度调节"):"等待读取相机设置"
        }
    }
}
