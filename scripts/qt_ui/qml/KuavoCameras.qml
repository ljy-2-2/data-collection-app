import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Collection.Native 1.0

Card {
    id: root
    property var status: ({})
    property bool active: true
    property var config: status.config||({transport:"network",host:"192.168.26.22",user:"jt",ros_master:"http://192.168.26.1:11311",ros_setup:"/opt/ros/noetic/setup.bash",roles:{}})
    property var roles: ["head","left","right"]
    property var labels: ["头部主相机","左相机","右相机"]
    property bool busy: !!backend.state.kuavoCameraBusy||!!status.reconnecting
    function camera(role) {return (status.cameras||[]).find(c=>c.id===role)||({})}
    Heading {width:parent.width;height:51;title:"相机画面"}
    Row {anchors.right:parent.right;anchors.rightMargin:12;y:9;spacing:6
        Btn {objectName:"kuavoCameraSettings";text:"相机参数";glyph:"Settings2";textSize:12;enabled:!root.busy;onClicked:settings.open()}
    }
    Item {id:images;clip:true;x:10;y:61;width:parent.width-20;height:parent.height-71
        Repeater {model:root.roles
            Rectangle {
                required property string modelData
                required property int index
                property var entry:root.camera(modelData)
                objectName:"kuavoCameraTile-"+modelData
                x:index===0?0:(images.width-8)*.66+8
                y:index===2?(images.height+8)/2:0
                width:(images.width-8)*(index===0?.66:.34)
                height:index===0?images.height:(images.height-8)/2
                radius:8;color:"black";clip:true
                CameraItem {id:video;clip:true;objectName:"kuavoCameraVideo-"+modelData;anchors.fill:parent;visible:online;source:root.active&&entry.connected?backend.baseUrl+"/kuavo/cameras/"+modelData+"/stream?v="+(root.status.stream_id||""):""}
                Txt {anchors.centerIn:parent;width:parent.width-16;horizontalAlignment:Text.AlignHCenter;wrapMode:Text.Wrap;visible:!video.online;text:root.status.reconnecting?"连接中…":entry.state==="error"?"连接异常":entry.state==="waiting"?"连接中…":"未连接";color:"#8793a5";font.pixelSize:11}
                Rectangle {x:6;y:6;height:24;width:label.implicitWidth+12;radius:5;color:"#99000000"
                    Txt {id:label;anchors.centerIn:parent;text:root.labels[index];font.pixelSize:12;color:"white"}
                }
            }
        }
    }
    KuavoCameraDialog {
        id:settings;cameraStatus:root.status;busy:root.busy
        editable:!((backend.state.kuavo||{}).collection||{}).busy
    }
}
