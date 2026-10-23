import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Column {
    id: root
    property var s: backend.state
    property var k: s.kuavo
    property var service: (k.service||{}).service||{}
    property bool activeSession: !!k.active || ["starting","running","stopping","unknown"].indexOf(k.state)>=0
    property bool locked: !!s.busy || activeSession || k.state==="checking"
    property string sessionLabel: ({idle:"未运行",checking:"检查中",starting:"准备中",running:"运行中",stopping:"结束中",error:"异常",unknown:"状态未知"}[k.state]||k.state)
    property string pendingAction: ""
    property bool showPreparation: false
    property bool showRecords: false
    spacing: 12
    Card {
        width: parent.width;height: setupGrid.height+footer.height
        GridLayout {
            id: setupGrid;width: parent.width;columns: app.width<=1180 ? 1 : 2;columnSpacing: 0;rowSpacing: 0
            Item {
                Layout.fillWidth: true;Layout.preferredWidth: 620;Layout.preferredHeight: modeColumn.height+24
                Column {
                    id: modeColumn;x:12;y:12;width:parent.width-24;spacing:10
                    RowLayout {width:parent.width;height:20;spacing:5
                        Txt {text:"01";font.pixelSize:11;color:Theme.accentStrong;Layout.preferredWidth:22}
                        Txt {text:"遥操作设置";font.weight:Font.DemiBold}
                        Item {Layout.fillWidth:true}
                        Txt {text:"乐聚 KUAVO 5-W";font.pixelSize:11;color:Theme.inkMuted}
                    }
                    Flow {width:parent.width;spacing:8
                        Column {width:140;spacing:5
                            Txt {text:"操作手臂";font.pixelSize:11;color:Theme.inkMuted}
                            Select {objectName:"kuavoArmMode";width:parent.width;model:["双臂","单臂 · 左臂","单臂 · 右臂"];currentIndex:["dual","left","right"].indexOf(s.kuavoArmMode);enabled:!root.locked;onActivated:backend.setValue("kuavoArmMode",["dual","left","right"][currentIndex])}
                        }
                        NumberField {objectName:"kuavoScale";width:120;showRange:false;spec:({label:"位移倍率",unit:"倍",min:.1,max:1,step:.1});value:s.kuavoPositionScale;enabled:!root.locked;onEdited:n=>backend.setValue("kuavoPositionScale",n)}
                        Column {width:120;spacing:5
                            Txt {text:"速度 / 加速度";font.pixelSize:11;color:Theme.inkMuted}
                            Txt {height:34;text:k.reference? k.reference.max_velocity_rad_s+" / "+k.reference.planning_acceleration_rad_s2:"—";font.pixelSize:13}
                        }
                        Btn {objectName:"kuavoOtherParameters";text:"其他参数";glyph:"Settings2";enabled:!root.locked;onClicked:gripperDialog.open()}
                    }
                    Txt {width:parent.width;wrapMode:Text.Wrap;text:"手臂速度 rad/s、加速度 rad/s² 沿用乐聚正式配置；夹爪设置在其他参数中修改。";font.pixelSize:11;color:Theme.inkMuted}
                    Txt {visible:s.kuavoArmMode!=="dual";width:parent.width;wrapMode:Text.Wrap;text:"单臂模式：另一侧 Grip / Trigger 无效；Y 双臂回位禁用，B 取消仍有效。";font.pixelSize:11;color:Theme.amber}
                    Flow {width:parent.width;spacing:8
                        Select {width:Math.min(230,parent.width);model:["本电脑网络（默认）","原项目专用网络 kuavons"];currentIndex:s.kuavoNamespace==="current"?0:1;enabled:!root.locked;onActivated:backend.setValue("kuavoNamespace",currentIndex===0?"current":"kuavons")}
                        Btn {objectName:"kuavoConnection";text:"机器人连接";glyph:"Settings2";enabled:!root.locked;onClicked:connectionDialog.open()}
                    }
                    Txt {width:parent.width;wrapMode:Text.Wrap;text:(k.connection||{}).configured?"已配置机器人地址："+k.connection.host+":"+k.connection.port:"新电脑首次使用，请先配置机器人连接。";font.pixelSize:11;color:Theme.inkMuted}
                    Txt {visible:!!(k.gripper_settings||{}).saved;width:parent.width;wrapMode:Text.Wrap;text:(k.gripper_settings||{}).message||"";font.pixelSize:11;color:Theme.inkMuted}
                    RowLayout {width:parent.width;spacing:7
                        Tick {text:"";checked:s.kuavoBusAccepted;enabled:!root.locked;onClicked:backend.setValue("kuavoBusAccepted",checked)}
                        Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:"接受原项目夹爪与手臂共用 CAN 总线的使用条件（兼容性尚未完成验证）";font.pixelSize:12;color:Theme.inkMuted}
                    }
                }
            }
            Item {
                Layout.fillWidth:true;Layout.preferredWidth:375;Layout.preferredHeight:devices.height+24
                Rectangle {width:setupGrid.columns===1?parent.width:1;height:setupGrid.columns===1?1:parent.height;color:Theme.line}
                Column {
                    id:devices;x:12;y:12;width:parent.width-24;spacing:8
                    Devices {
                        objectName:"kuavoDevices";width:parent.width
                        checking:s.busy==="check" || k.state==="checking"
                        property bool statusKnown:["unknown","error"].indexOf(k.state)<0
                        items:[
                            {id:"robot",name:"乐聚 KUAVO 5-W",status:statusKnown && !!(k.service||{}).management_ready?"connected":"offline",detail:"最近一次检查的机器人服务连接与版本核验结果，不代表电机已使能。"},
                            {id:"xr",name:"Galaxis VR",status:(root.activeSession?!!k.pico_live:!!k.vr_ready)?"connected":"offline",detail:root.activeSession?(k.pico_live?"正在接收实时头显输入":"等待新的头显输入"):(k.vr_detail||"尚未检查头显输入")},
                            {id:"headset",name:"Pico 设备",status:(root.activeSession?!!k.pico_live:!!k.pico_ready)?"connected":"offline",detail:k.pico_detail||"尚未检查 PICO USB 和头显应用"}
                        ]
                    }
                    Txt {width:parent.width;wrapMode:Text.Wrap;text:"点击检查设备会唤醒 PICO、核验 USB 和 VR 输入，必要时恢复本机 VR 接收服务；空闲时显示最近检查结果，遥操中显示实时输入状态。";font.pixelSize:11;color:Theme.inkMuted}
                    Txt {visible:!!k.vr_checked && (!k.vr_ready || !k.pico_ready);width:parent.width;wrapMode:Text.Wrap;text:[k.pico_ready?"":k.pico_detail,k.vr_ready?"":k.vr_detail].filter(v=>!!v).join("；");font.pixelSize:11;color:Theme.amber}
                }
            }
        }
        Rectangle {
            id:footer;y:setupGrid.height;width:parent.width;height:footerFlow.implicitHeight+12;color:Theme.muted
            Rectangle {width:parent.width;height:1;color:Theme.line}
            Flow {id:footerFlow;x:12;y:6;width:parent.width-24;spacing:6;layoutDirection:Qt.RightToLeft
                Btn {objectName:"kuavoStart";visible:!root.activeSession;text:"开始遥操作";glyph:"Play";primary:true;enabled:!root.locked && !!(k.service||{}).management_ready && service.ready_to_start===true && k.pico_sdk_available && k.vr_ready && k.pico_ready && s.kuavoBusAccepted && (!(k.gripper_settings||{}).saved || !!k.gripper_settings.matches_service);onClicked:{root.pendingAction="execute";confirmation.open()}}
                Btn {objectName:"kuavoStop";visible:root.activeSession;text:k.state==="stopping"?"正在结束…":"结束遥操作";glyph:"Square";primary:true;danger:true;enabled:root.activeSession && !!k.session_id && k.state!=="stopping";onClicked:backend.teleop("stop")}
                Btn {objectName:"kuavoCheck";text:"检查设备";glyph:"RefreshCw";busy:s.busy==="check";enabled:!root.locked;onClicked:backend.teleop("check")}
                Txt {height:34;text:"会话："+root.sessionLabel+"  ·  本地日志自动保存";font.pixelSize:12;color:Theme.inkMuted}
            }
        }
    }
    GridLayout {
        id:visualGrid;objectName:"kuavoVisualGrid";width:parent.width;columns:width<1080?1:2;columnSpacing:12;rowSpacing:12
        Card {
            objectName:"kuavoRobotCard";Layout.fillWidth:true;Layout.preferredWidth:444;Layout.maximumWidth:visualGrid.columns===2?504:Infinity;Layout.preferredHeight:340
            Heading {width:parent.width;height:51;title:"机器人姿态"}
            Txt {anchors.right:parent.right;anchors.rightMargin:14;y:17;text:"● "+(k.pose_live?"双臂遥测在线":k.pose_available?"反馈已过期":"参考姿态");font.pixelSize:11;color:Theme.inkSoft}
            Rectangle {x:10;y:61;width:parent.width-20;height:parent.height-71;radius:10;border.color:Theme.line;color:Theme.stage;clip:true
                Loader {anchors.fill:parent;active:root.visible;source:"KuavoPose.qml"}
                Txt {anchors.bottom:parent.bottom;anchors.horizontalCenter:parent.horizontalCenter;anchors.bottomMargin:8;text:"官方 v63 模型 · 非手臂部位为参考零位";font.pixelSize:10;color:Theme.inkMuted}

            }
        }
        Card {
            objectName:"kuavoCameraCard";Layout.fillWidth:true;Layout.preferredWidth:716;Layout.preferredHeight:340
            Heading {width:parent.width;height:51;title:"相机预览"}
            Txt {anchors.right:parent.right;anchors.rightMargin:14;y:17;text:"待接入";font.pixelSize:11;color:Theme.inkSoft}
            RowLayout {x:10;y:61;width:parent.width-20;height:parent.height-71;spacing:8
                Rectangle {Layout.fillWidth:true;Layout.preferredWidth:2;Layout.fillHeight:true;radius:8;color:"#101114"}
                ColumnLayout {Layout.fillWidth:true;Layout.preferredWidth:1;Layout.fillHeight:true;spacing:8
                    Rectangle {Layout.fillWidth:true;Layout.fillHeight:true;radius:8;color:"#101114"}
                    Rectangle {Layout.fillWidth:true;Layout.fillHeight:true;radius:8;color:"#101114"}
                }
            }
        }
    }
    Card {
        width:parent.width;height:manual.height+59
        Heading {width:parent.width;height:47;title:"手动控制"}
        Column {id:manual;x:12;y:59;width:parent.width-24;spacing:10
            Rectangle {width:parent.width;height:prep.implicitHeight+20;radius:10;color:Theme.muted;border.color:Theme.line
                ColumnLayout {id:prep;x:10;y:10;width:parent.width-20;spacing:8
                    Txt {text:"主控与夹爪准备";font.weight:Font.DemiBold}
                    Flow {Layout.fillWidth:true;spacing:6
                        Repeater {model:[{key:"start",label:"启动主控"},{key:"initialize",label:"厂家初始化"},{key:"grippers_inspect",label:"读取夹爪状态"},{key:"grippers_prepare",label:"准备夹爪"}]
                            Btn {required property var modelData;text:modelData.label;enabled:!root.locked && !!(k.service||{}).management_ready && (modelData.key.indexOf("grippers")!==0 || s.kuavoBusAccepted);onClicked:{root.pendingAction=modelData.key;confirmation.open()}}
                        }
                    }
                    Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:"沿用乐聚准备接口；已有主控时不要重复启动。检查设备仅查询服务和准备状态。";font.pixelSize:12;color:Theme.inkMuted}
                    Btn {text:root.showPreparation?"收起准备结果":"查看准备结果";visible:Object.keys(k.preparation||{}).length>0;onClicked:root.showPreparation=!root.showPreparation}
                    Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:JSON.stringify(k.preparation||{},null,2);font.pixelSize:11;color:Theme.inkMuted;visible:root.showPreparation}
                }
            }
            Flow {width:parent.width;spacing:6
                Btn {text:"关节微调 · 待接入";enabled:false}
                Btn {text:"姿态保存 / 复位 · 待接入";enabled:false}
            }
            Txt {width:parent.width;wrapMode:Text.Wrap;text:"独立手动控制接口尚未接入；双臂会话沿用 Y 回位 / B 取消，单臂禁用 Y。";font.pixelSize:12;color:Theme.inkMuted}
            Item {width:1;height:2}
        }
    }
    Card {
        width:parent.width;height:guide.implicitHeight+24
        ColumnLayout {id:guide;x:12;y:12;width:parent.width-24;spacing:8
            Txt {text:"手柄操作 · 使用乐聚原映射";font.weight:Font.DemiBold}
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:"Grip：控制同侧手臂，两侧可同时握持。Trigger：独立接管夹爪，0 张开、1 闭合。\n开始时先松开 Grip 与 Trigger。左 Y：双臂模式下松开两侧 Grip 和 Trigger 后回位（单臂禁用）；右 B：取消回位。\nA/X 不映射为天机 Marvin M6 的采集按钮；结束会话请使用本页按钮。";color:Theme.inkMuted}
        }
    }
    Card {
        width:parent.width;height:records.implicitHeight+24
        ColumnLayout {id:records;x:12;y:12;width:parent.width-24;spacing:10
            RowLayout {Layout.fillWidth:true
                Txt {text:"本地记录";font.weight:Font.DemiBold;Layout.fillWidth:true}
                Btn {text:root.showRecords?"收起详情":"日志与待接入说明";onClicked:root.showRecords=!root.showRecords}
            }
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:k.log_directory?"会话日志："+k.log_directory:"启动会话后自动保存输入、机器人回执、退出报告；暂不生成同步图像数据集或上传平台。";color:Theme.inkMuted}
            ColumnLayout {Layout.fillWidth:true;visible:root.showRecords;spacing:8
                Repeater {model:k.limitations||[]
                    Txt {required property string modelData;Layout.fillWidth:true;wrapMode:Text.Wrap;text:"• "+modelData;color:Theme.inkMuted}
                }
                Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:(k.logs||[]).slice(-12).join("\n")||"暂无日志";font.pixelSize:11}
            }
        }
    }
    KuavoGripperDialog {id:gripperDialog;settings:k.gripper_settings||({});fallback:(k.reference||{}).grippers||({});editable:!root.locked}
    KuavoConnectionDialog {id:connectionDialog;connection:k.connection||({});editable:!root.locked}
    Dialog {
        id:confirmation;objectName:"kuavoConfirm";parent:Overlay.overlay;anchors.centerIn:parent;modal:true;width:Math.min(520,app.width-32)
        title:root.pendingAction==="execute"?"开始乐聚遥操作":"确认乐聚准备操作"
        standardButtons:Dialog.Ok|Dialog.Cancel
        onOpened:motorIds.checked=false
        onAccepted:{if(root.pendingAction==="execute")backend.teleop("start");else backend.kuavoPrepare(root.pendingAction,motorIds.checked)}
        contentItem:ColumnLayout {spacing:12
            Txt {Layout.fillWidth:true;wrapMode:Text.Wrap;text:root.pendingAction==="grippers_inspect"?"读取会发送夹爪查询帧，不移动或改写参数。":"此操作由你手动发起，可能启动控制或产生动作。请确认现场已按厂家流程准备，且没有其它遥操会话。"}
            Tick {id:motorIds;visible:root.pendingAction==="start";text:"已确认左电机 8、右电机 1、反馈编号 0"}
        }
    }
}
