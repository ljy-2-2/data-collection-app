import QtQuick
import QtQuick.Controls
Rectangle {
    id: pad
    objectName: "kuavoBaseDisc"
    property bool canMove: false
    property string held: ""
    signal directionPressed(string direction)
    signal directionReleased()
    width: 220;height: 220;radius: width/2
    color: Theme.muted;border.color: Theme.line
    Repeater {
        model: [
            {key:"forward",label:"前进",angle:-90,icon:"ArrowRight",rotation:-90,x:110,y:28},
            {key:"right",label:"右移",angle:0,icon:"ArrowRight",rotation:0,x:192,y:110},
            {key:"backward",label:"后退",angle:90,icon:"ArrowRight",rotation:90,x:110,y:192},
            {key:"left",label:"左移",angle:180,icon:"ArrowRight",rotation:180,x:28,y:110},
            {key:"turn_left",label:"左转",angle:0,icon:"RotateCcw",rotation:0,x:84,y:110},
            {key:"turn_right",label:"右转",angle:0,icon:"RotateCw",rotation:0,x:136,y:110}
        ]
        Button {
            id: control
            required property var modelData
            property bool turning: modelData.key.indexOf("turn_")===0
            property bool selected: pad.held===modelData.key
            property color fillColor: selected ? Theme.accentSoft : hovered && enabled ? Theme.raised : Theme.surface
            property color edgeColor: selected || activeFocus ? Theme.accentStrong : Theme.line
            objectName: "kuavoBase_"+modelData.key
            anchors.fill: parent;padding:0;hoverEnabled:true;enabled:pad.canMove
            opacity:enabled?1:.45
            Accessible.name:"底盘"+modelData.label
            // Match pointer hit regions to the drawn sectors, including the gaps.
            containmentMask: QtObject {
                function contains(point: point): bool {
                    let dx=point.x-110,dy=point.y-110,r=Math.sqrt(dx*dx+dy*dy)
                    if(control.turning)return r<=49 && (control.modelData.key==="turn_left"?dx<=-2:dx>=2)
                    let angle=Math.atan2(dy,dx)*180/Math.PI-control.modelData.angle
                    angle=(angle+540)%360-180
                    return r>=57 && r<=105 && Math.abs(angle)<=42
                }
            }
            background: Canvas {
                id: sector
                onPaint: {
                    let ctx=getContext("2d");ctx.reset();ctx.fillStyle=control.fillColor;ctx.strokeStyle=control.edgeColor;ctx.lineWidth=1
                    ctx.beginPath()
                    if(control.turning) {
                        let left=control.modelData.key==="turn_left",a=Math.acos(2/49)
                        if(left)ctx.arc(110,110,49,Math.PI-a,Math.PI+a,false)
                        else ctx.arc(110,110,49,-a,a,false)
                    } else {
                        let start=(control.modelData.angle-42)*Math.PI/180,end=(control.modelData.angle+42)*Math.PI/180
                        ctx.arc(110,110,105,start,end,false)
                        ctx.arc(110,110,57,end,start,true)
                    }
                    ctx.closePath();ctx.fill();ctx.stroke()
                }
                Connections {target:control;function onFillColorChanged(){sector.requestPaint()}function onEdgeColorChanged(){sector.requestPaint()}}
            }
            contentItem: Item {
                Image {x:control.modelData.x-12;y:control.modelData.y-12;width:24;height:24;sourceSize.width:48;sourceSize.height:48
                    source:Theme.icon(control.modelData.icon,"ink");rotation:control.modelData.rotation}
            }
            onPressed:pad.directionPressed(modelData.key)
            onReleased:pad.directionReleased()
            onCanceled:pad.directionReleased()
            onActiveFocusChanged:if(!activeFocus && selected)pad.directionReleased()
        }
    }
}
