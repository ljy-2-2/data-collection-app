import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Collection.Native 1.0
Column {
    id: root
    property var s: backend.state
    property var status: s.umi
    property var check: (status.monitor||{}).device_check || {}
    property var candidates: check.camera_candidates || []
    property var candidateLabels: ["请选择设备"].concat(candidates.map(function(device) {
        let detail=(check.camera_candidate_details||[]).find(d=>d.device===device)
        return detail ? device+" · "+detail.name : device
    }))
    property var roles: ["left","center","right"]
    property var titles: ["左腕相机","中心相机","右腕相机"]
    property var sides: s.profile==="dual" ? ["left","right"] : [s.profile.split("-")[1]]
    property bool ready: status.state==="ready" && status.monitor_connected && !s.busy
    property bool recording: !!status.recording
    readonly property bool recordDialogOpen: discardDialog.visible
    property bool active: ["idle","error"].indexOf(status.state)<0
    property bool resolutionBusy: Object.values(backend.umiCameraResolution).some(v=>v.busy)
    // Use the full content width on both pages, with the same square previews.
    readonly property int cameraColumns: app.width<780 ? 1 : 3
    property bool canStart: !resolutionBusy && s.checkedProfile===s.profile && !check.stack_occupied && !!check.hardware_ready && !!check.pico_ready && sides.every(side=>s.minima[side]!==undefined && s.encoders[side] && s.encoders[side].online) && roles.filter(r=>r==="center" || sides.indexOf(r)>=0).every(r=>s.cameraRoles[r] && s.cameraLive[r]) && !s.busy
    function recordingDuration(seconds) {
        const total = Math.max(0, Math.floor(Number(seconds)||0))
        return String(Math.floor(total/60)).padStart(2,"0")+":"+String(total%60).padStart(2,"0")
    }
    spacing: 12
    Setup {visible: s.umiStep===1;width: parent.width;umi: true}
    Card {
        visible: s.umiStep===1 && ((root.status.pending_processing||0)>0 || (root.status.pending_imports||0)>0 || (root.status.failed_processing||0)>0)
        width: parent.width;height: idleUploads.implicitHeight+24
        Column {id: idleUploads;x: 12;y: 12;width: parent.width-24;spacing: 5
            Txt {text: "后台处理与上传";font.weight: Font.DemiBold}
            Txt {width: parent.width;text: "采集已结束，后台加速处理与上传；关闭软件后也会继续。";wrapMode: Text.Wrap;elide: Text.ElideNone;font.pixelSize: 12}
            Txt {width: parent.width;text: "视频压缩与校验 "+(root.status.pending_processing||0)+" 段 · 待上传 "+(root.status.pending_imports||0)+" 段";color: Theme.inkSoft;font.pixelSize: 11}
            Txt {visible: (root.status.failed_processing||0)>0;width: parent.width;text: "处理失败 "+(root.status.failed_processing||0)+" 段，请重试处理/上传";color: Theme.red;wrapMode: Text.Wrap;elide: Text.ElideNone}
            Btn {text: "重试处理/上传";ghost: true;enabled: !s.busy;onClicked: backend.retryUploads()}
        }
    }
    Card {
        visible: s.umiStep===2;width: parent.width;height: preflight.height+heading.height+24
        Heading {id: heading;width: parent.width;eyebrow: "STEP 02";title: "检查并选择硬件"}
        Column {id: preflight;x: 12;y: heading.height+12;width: parent.width-24;spacing: 10
            UploadTarget {width: parent.width;locked: root.active}
            GridLayout {width: parent.width;columns: app.width<620?2:4;columnSpacing: 6;rowSpacing: 6
                Repeater {model: ["相机","编码器","Galaxis VR","PICO"]
                    Rectangle {required property string modelData;required property int index;Layout.fillWidth: true;height: 52;color: Theme.muted;radius: 10;border.color: Theme.line
                        Image {x: 8;anchors.verticalCenter: parent.verticalCenter;width: 19;height: 19;source: Theme.icon(["Camera","Settings2","Wifi","Glasses"][index],"accent")}
                        Column {x: 35;y: 8;spacing: 2
                            Txt {text: modelData;font.pixelSize: 12;color: Theme.inkSoft;height: 15}
                            Txt {text: index===0?Math.min(root.candidates.length,root.sides.length+1)+"/"+(root.sides.length+1):index===1?Math.min((root.check.encoder_candidates||[]).length,root.sides.length)+"/"+root.sides.length:index===2?root.check.vr_service_ready?"已启动":"未启动":root.check.pico_ready?"已连接":"未连接";font.pixelSize: 13;font.weight: Font.DemiBold;height: 18}
                        }
                    }
                }
            }
            Rectangle {width: parent.width;height: 54;radius: 10;color: Theme.muted;border.color: Theme.line
                Txt {x: 10;anchors.verticalCenter: parent.verticalCenter;text: "硬件检测";font.weight: Font.DemiBold}
                Btn {anchors.right: parent.right;anchors.rightMargin: 10;anchors.verticalCenter: parent.verticalCenter;objectName: "umiCheck";text: Object.keys(root.check).length?"重新检测":"开始检测";glyph: "RefreshCw";busy: s.busy==="check-umi";enabled: !s.busy && !root.active;onClicked: backend.checkUmi()}
            }
            Column {visible: Object.keys(root.check).length>0;width: parent.width;spacing: 10
                Rectangle {width: parent.width;height: 1;color: Theme.line}
                GridLayout {objectName: "umiPreflightCameras";width: parent.width;columns: root.cameraColumns;columnSpacing: 9;rowSpacing: 9
                    Repeater {model: 3
                        Rectangle {required property int index;property string role: root.roles[index];property bool active: role==="center" || root.sides.indexOf(role)>=0
                            objectName: "umiPreflightCard-"+role
                            Layout.fillWidth: true;Layout.minimumWidth: 0;Layout.preferredWidth: 1;Layout.preferredHeight: width+41+preflightControls.height;radius: 9;color: Theme.muted;border.color: Theme.line;opacity: active?1:.55
                            RowLayout {x: 7;y: 5;width: parent.width-14;height: 32;spacing: 8
                                Txt {text: root.titles[index];font.pixelSize: 12;font.weight: Font.DemiBold;color: Theme.inkMuted}
                                Select {Layout.fillWidth: true;height: 30;font.pixelSize: 11;model: root.candidateLabels;currentIndex: root.candidates.indexOf(s.cameraRoles[role]||"")+1;enabled: parent.parent.active && !s.busy;onActivated: backend.cameraRole(role,currentIndex>0?root.candidates[currentIndex-1]:"")}
                            }
                            Rectangle {x: 1;y: 42;width: parent.width-2;height: parent.height-43-preflightControls.height;color: Theme.dark?"#15202a":"#f5f6fc"
                                CameraItem {id: camera;objectName: "umiPreflightVideo-"+role;anchors.fill: parent;cropToFill: true;source: root.visible && s.page==="umi" && s.umiStep===2 && !s.busy && parent.parent.active && s.cameraRoles[role] ? backend.baseUrl+"/umi/device-cameras/"+encodeURIComponent(s.cameraRoles[role].replace(/^\/dev\//,"")) : "";onOnlineChanged: {let activeRole=role;let isOnline=online;Qt.callLater(function(){backend.cameraReady(activeRole,isOnline)})}}
                                Txt {anchors.centerIn: parent;visible: !camera.online;text: parent.parent.active ? s.busy==="check-umi"?"检测中":"等待相机接入" : "未启用";font.pixelSize: 11;color: Theme.inkSoft}
                            }
                            Column {id: preflightControls;anchors.bottom: parent.bottom;width: parent.width
                                CameraResolution {width: parent.width;height: implicitHeight;cameraKey: (s.cameraRoles[role]||"").replace(/^\/dev\//,"");cameraName: root.titles[index];available: root.visible && s.page==="umi" && s.umiStep===2 && !s.busy && parent.parent.active && !!cameraKey}
                                CameraBrightness {id: preflightBrightness;width: parent.width;height: implicitHeight;cameraKey: (s.cameraRoles[role]||"").replace(/^\/dev\//,"");cameraName: root.titles[index];active: parent.parent.active;available: root.visible && s.page==="umi" && s.umiStep===2 && !s.busy && active && camera.online}
                            }
                        }
                    }
                }
                GridLayout {width: parent.width;columns: app.width<620?1:2;columnSpacing: 9;rowSpacing: 9
                    Sensor {Layout.fillWidth: true;side: "left";active: root.sides.indexOf(side)>=0}
                    Sensor {Layout.fillWidth: true;side: "right";active: root.sides.indexOf(side)>=0}
                }
            }
            Txt {visible: !!root.check.stack_occupied;width: parent.width;text: "其他 Galaxis 服务正在占用相机和编码器。请先结束该服务，再重新检测。";font.pixelSize: 12;color: Theme.red;wrapMode: Text.Wrap;elide: Text.ElideNone}
            Txt {visible: !!root.check.pico_detail && !root.check.pico_ready;width: parent.width;text: root.check.pico_detail||"";font.pixelSize: 12;color: Theme.red;wrapMode: Text.Wrap;elide: Text.ElideNone}
            RowLayout {width: parent.width;spacing: 8
                Btn {text: "返回配置";ghost: true;enabled: !s.busy;onClicked: backend.umiBack()}
                Item {Layout.fillWidth: true}
                Btn {objectName: "umiStart";text: root.canStart ? "进入监控" : "确认并勾选";primary: true;iconAfter: true;glyph: "ArrowRight";enabled: root.canStart;busy: ["configure-umi","start-umi"].indexOf(s.busy)>=0;onClicked: backend.startUmi()}
            }
        }
    }
    Card {
        visible: s.umiStep===3;width: parent.width;height: monitor.height+24
        Column {id: monitor;x: 12;y: 12;width: parent.width-24;spacing: 9
            RowLayout {id: monitorHeader;width: parent.width;height: 24;spacing: 12
                Txt {text: "采集监控";font.pixelSize: 15;font.weight: Font.DemiBold}
                Txt {Layout.fillWidth: true;text: s.umiTask+" · "+(s.profile==="dual"?"双臂":s.profile==="single-left"?"仅左臂":"仅右臂");font.pixelSize: 12;color: Theme.inkMuted}
                Txt {text: "● "+(root.recording?"录制中":root.ready?"已就绪":root.active?(root.status.monitor_connected?"等待数据就绪":"启动中"):"未启动");font.pixelSize: 12;color: root.recording?Theme.red:root.ready?Theme.green:Theme.inkMuted}
            }
            GridLayout {id: monitorCameras;objectName: "umiMonitorCameras";width: parent.width;columns: root.cameraColumns;columnSpacing: 9;rowSpacing: 9
                Repeater {model: 3
                    Rectangle {required property int index;property string role: root.roles[index];property bool active: role==="center" || root.sides.indexOf(role)>=0
                        objectName: "umiMonitorCard-"+role
                        Layout.fillWidth: true;Layout.minimumWidth: 0;Layout.preferredWidth: 1
                        Layout.preferredHeight: width+33;radius: 9;border.color: Theme.line;color: Theme.muted;opacity: active?1:.55
                        RowLayout {x: 9;y: 3;width: parent.width-55;height: 28;spacing: 6
                            Txt {text: root.titles[index];color: Theme.accentStrong;font.pixelSize: 12;font.weight: Font.DemiBold}
                            Select {objectName: "umiLiveCameraSelect";Layout.fillWidth: true;height: 26;font.pixelSize: 10
                                model: root.candidateLabels
                                currentIndex: root.candidates.indexOf((root.check.camera_roles||{})[role]||"")+1
                                enabled: parent.parent.active && root.active && !root.recording && !s.busy
                                onActivated: {if(currentIndex>0)backend.selectLiveCamera(role,root.candidates[currentIndex-1])}
                            }
                        }
                        Txt {anchors.right: parent.right;anchors.rightMargin: 9;y: 9;text: liveCamera.online?"LIVE":"--";font.pixelSize: 10;color: liveCamera.online?Theme.green:Theme.inkSoft}
                        Rectangle {x: 1;y: 34;width: parent.width-2;height: parent.height-35;color: Theme.dark?"#15202a":"#f5f6fc"
                            // Fill the same square with all three images without
                            // stretching. Edge cropping affects only this preview.
                            CameraItem {id: liveCamera;objectName: "umiMonitorVideo-"+role;anchors.fill: parent;cropToFill: true;source: root.visible && s.page==="umi" && s.umiStep===3 && root.active && parent.parent.active && s.busy!=="stop-umi" ? backend.baseUrl+"/umi/cameras/"+role+"?native=true" : ""}
                            Rectangle {anchors.fill: parent;visible: !liveCamera.online && liveCamera.hasFrame;color: "#99000000"}
                            Txt {visible: !liveCamera.online;anchors.centerIn: parent;text: !parent.parent.active?"未启用":liveCamera.hasFrame?"画面中断，正在重连…":"等待视频流";font.pixelSize: 11;color: liveCamera.hasFrame?"white":Theme.inkSoft}
                        }

                    }
                }
            }
            Rectangle {id: vrPanel;width: parent.width;height: vrColumn.height+14;color: Theme.muted;radius: 9;border.color: Theme.line
                Column {id: vrColumn;x: 9;y: 7;width: parent.width-18;spacing: 5
                    Txt {text: "VR 状态";font.pixelSize: 12;font.weight: Font.DemiBold;color: Theme.inkMuted}
                    GridLayout {width: parent.width;columns: app.width<620?1:3;columnSpacing: 6;rowSpacing: 6
                        Repeater {model: ["left","hmd","right"]
                            Rectangle {required property string modelData;required property int index;property var reading: (backend.umiTelemetry.vr||{})[modelData] || {};property bool online: !!backend.umiTelemetry.monitor_connected && !!reading.online && reading.rate_hz>0
                                Layout.fillWidth: true;height: 67;color: Theme.surface;border.color: Theme.line;radius: 6;opacity: online?1:.5
                                RowLayout {x: 7;y: 5;width: parent.width-14
                                    Txt {text: ["左手柄","头显","右手柄"][index];font.pixelSize: 11;font.weight: Font.DemiBold;Layout.fillWidth: true}
                                    Txt {text: parent.parent.online ? parent.parent.reading.tracked===false || parent.parent.reading.valid===false ? "追踪丢失" : "在线" : "无数据";font.pixelSize: 9;color: Theme.inkSoft}
                                    Txt {text: parent.parent.online ? Number(parent.parent.reading.rate_hz).toFixed(1)+" Hz" : "-- Hz";font.pixelSize: 9;color: Theme.inkSoft}
                                }
                                Column {x: 7;y: 27;spacing: 2;width: parent.width-14
                                    Txt {width: parent.width;text: "位置  "+(parent.parent.online?JSON.stringify(parent.parent.reading.position||[])+' m':"--");font.pixelSize: 10;color: Theme.inkMuted}
                                    Txt {width: parent.width;text: "姿态  "+(parent.parent.online?JSON.stringify(parent.parent.reading.rotation||[]):"--");font.pixelSize: 10;color: Theme.inkMuted}
                                }
                            }
                        }
                    }
                }
            }
            GridLayout {id: monitorDetails;objectName: "umiMonitorDetails";width: parent.width;columns: app.width<1000?1:2;columnSpacing: 9;rowSpacing: 9
                Rectangle {Layout.fillWidth: true;Layout.preferredHeight: 198;color: Theme.muted;border.color: Theme.line;radius: 9
                    Txt {x: 9;y: 7;text: "磁编码器";font.pixelSize: 12;font.weight: Font.DemiBold;color: Theme.inkMuted}
                    RowLayout {x: 9;y: 30;width: parent.width-18;spacing: 6
                        Sensor {Layout.fillWidth: true;side: "left";monitoring: true;active: root.sides.indexOf(side)>=0}
                        Sensor {Layout.fillWidth: true;side: "right";monitoring: true;active: root.sides.indexOf(side)>=0}
                    }
                }
                Rectangle {Layout.fillWidth: true;Layout.preferredHeight: Math.max(198,recordActions.y+recordActions.implicitHeight+10);color: Theme.muted;border.color: Theme.line;radius: 9
                    Txt {x: 10;y: 8;text: "采集统计 · "+(s.uploadTarget.selected==="local"?"上传本机平台":"上传云端平台");font.pixelSize: 12;font.weight: Font.DemiBold;color: Theme.inkMuted}
                    RowLayout {x: 10;y: 37;width: parent.width-20;height: 62;spacing: 6
                        Repeater {model: ["本次完成","当前回合","本回合录制时长","待上传"]
                            Column {required property string modelData;required property int index;Layout.fillWidth: true;Layout.preferredWidth: 1;Layout.minimumWidth: 0;spacing: 3
                                Txt {width: parent.width;text: modelData;font.pixelSize: 10;color: Theme.inkSoft}
                                Txt {objectName: index===2?"umiEpisodeDuration":"";width: parent.width;text: index===0?String(root.status.episodes_completed||0):index===1?((root.status.monitor||{}).episode||{}).label||"—":index===2?root.recordingDuration(((root.status.monitor||{}).episode||{}).elapsed_sec):String(root.status.pending_imports||0);font.pixelSize: index===0?20:12;color: index===0?Theme.accentStrong:Theme.ink;font.weight: Font.DemiBold}
                                Txt {text: index===0?"Episode":"";font.pixelSize: 9;color: Theme.inkSoft}
                            }
                        }
                    }
                    Rectangle {y: 104;width: parent.width;height: 1;color: Theme.line}
                    Column {id: recordActions;x: 10;y: 113;spacing: 3;width: parent.width-20
                        Txt {text: root.recording?"正在录制":root.ready?"可以开始采集":root.status.monitor_connected?"等待设备实时数据稳定":"启动中";font.pixelSize: 12;font.weight: Font.DemiBold}
                        Txt {text: root.recording?"手柄 B/Y 或空格踏板保存本段":"手柄 A/X 或空格踏板开始";font.pixelSize: 10;color: Theme.inkSoft}
                        Txt {objectName: "umiPedalFeedback";width: parent.width;text: s.umiPedalMessage || "空格踏板：保持本窗口在前台，每踩一次切换录制";font.pixelSize: 9;color: Theme.inkSoft;wrapMode: Text.Wrap;elide: Text.ElideNone}
                        Txt {width: parent.width;visible: (root.status.pending_processing||0)>0 || (root.status.failed_processing||0)>0;text: "后台视频压缩与校验 " + (root.status.pending_processing||0) + " 段 · 处理失败 " + (root.status.failed_processing||0) + " 段，可继续采集";font.pixelSize: 10;color: (root.status.failed_processing||0)>0?Theme.red:Theme.inkSoft;wrapMode: Text.Wrap}
                        Txt {width: parent.width;visible: !!text;text: (root.status.upload_queue||{}).message||"";font.pixelSize: 10;color: Theme.inkSoft;wrapMode: Text.Wrap;elide: Text.ElideNone}
                        Row {spacing: 6
                            Btn {objectName: "umiRecord";text: root.recording?"完成本段":"开始采集";glyph: root.recording?"Square":"Radio";primary: true;textSize: 11;height: 32;enabled: !s.busy && (root.recording||root.ready);onClicked: backend.recordUmi(root.recording?"stop":"start")}
                            Btn {text: root.recording?"放弃本段":"结束返回";ghost: true;danger: true;textSize: 11;height: 32;enabled: !s.busy;onClicked: {if(root.recording)discardDialog.open();else backend.stopUmi()}}
                            Btn {text: "重试处理/上传";ghost: true;textSize: 11;height: 32;enabled: !s.busy;onClicked: backend.retryUploads()}
                        }
                    }
                }
            }
        }
    }
    Dialog {id: discardDialog;parent: Overlay.overlay;anchors.centerIn: parent;modal: true;padding: 20;width: Math.min(400,app.width-32)
        background: Rectangle {color: Theme.surface;radius: 12;border.color: Theme.line}
        Overlay.modal: Rectangle {color: "#52000000"}
        contentItem: ColumnLayout {spacing: 18
            Txt {text: "确认放弃当前录制片段？"}
            RowLayout {Layout.alignment: Qt.AlignRight
                Btn {text: "取消";onClicked: discardDialog.close()}
                Btn {text: "放弃本段";danger: true;primary: true;onClicked: {backend.recordUmi("cancel");discardDialog.close()}}
            }
        }
    }
}
