#!/usr/bin/env python3
"""Offline CAD replay viewer. Reads saved simulation results only."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
from PySide6.QtCore import QObject,Property,Signal,Slot,QTimer,QUrl
from PySide6.QtGui import QGuiApplication,QVector3D
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from qt_ui.visuals import RobotScene,quat

ROOT=Path(__file__).resolve().parents[2]


class Replay(QObject):
    changed=Signal()
    def __init__(self,folder,scene):
        super().__init__();self.folder=folder;self.scene=scene
        self.run=json.loads((folder/'run.json').read_text())
        self.entries=json.loads((folder/'summary.json').read_text())['episodes']
        self.value=dict(theme='light',index=0,time=0.,duration=0.,playing=False,error='',status='',complete=False,initial='')
        self.targets=[];self.data=None;self.report={}
        self.timer=QTimer(self);self.timer.setInterval(33);self.timer.timeout.connect(self.tick)
        self.select(0)
    @Property('QVariantMap',notify=changed)
    def state(self):return self.value
    @Property('QStringList',constant=True)
    def labels(self):return [f"{r['row']:03d} · {r['folder']} · {'通过' if r['status']=='pass' else '未通过'}" for r in self.entries]
    @Property('QVariantList',notify=changed)
    def targetFrames(self):return self.targets
    @Slot(int)
    def select(self,index):
        if not 0<=index<len(self.entries):return
        self.timer.stop()
        self.report=self.entries[index]
        self.value.update(index=index,time=0.,playing=False,complete=self.report['status']=='pass',
                          error=self.report.get('error',''),status='完整位姿验证通过' if self.report['status']=='pass' else '未通过：仅查看诊断，不是可执行轨迹',
                          initial=(f"App 已保存姿态：{self.run['initial_pose'].get('pose_name','')} · 非实时反馈"
                                   if self.run['initial_pose'].get('source')=='saved_app_pose' else
                                   '实测关节快照' if self.run['initial_pose'].get('measured') else 'App 显示参考姿态 · 非实机实测'))
        path=self.folder/f"row-{self.report['row']:03d}"/'preview.npz'
        if self.data is not None:self.data.close()
        self.data=np.load(path,allow_pickle=False) if self.report.get('preview') and path.is_file() else None
        self.value['duration']=float(self.data['target_time_s'][-1]) if self.data is not None else 0.
        self.seek(0.)
    @Slot(float)
    def seek(self,t):
        t=max(0.,min(float(t),self.value['duration']));self.value['time']=t
        joints=self.run['initial_pose']['joints']
        self.targets=[]
        if self.data is not None:
            times=self.data['source_time_s'];q=self.data['joints_deg']
            joints={s:[float(np.interp(t,times,q[:,i,j])) for j in range(7)] for i,s in enumerate(('left','right'))}
            target_times=self.data['target_time_s'];target=self.data['target_tcp']
            index=int(np.clip(np.searchsorted(target_times,t),0,len(target_times)-1))
            display=np.asarray(self.run['display_from_world'])
            for i,s in enumerate(('left','right')):
                T=display@target[index,i]
                self.targets.append(dict(position=QVector3D(*T[:3,3].tolist()),orientation=quat(T)))
        self.scene.update_pose(dict(joints=joints))
        self.changed.emit()
    @Slot()
    def toggle(self):
        if not self.value['duration']:return
        if self.timer.isActive():self.timer.stop();self.value['playing']=False
        else:
            if self.value['time']>=self.value['duration']:self.seek(0.)
            self.timer.start();self.value['playing']=True
        self.changed.emit()
    def tick(self):
        stretch=1./self.report.get('plan',{}).get('actual_speed_ratio',1.)
        t=self.value['time']+.033/stretch
        self.seek(t)
        if t>=self.value['duration']:self.timer.stop();self.value['playing']=False;self.changed.emit()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('results',type=Path);p.add_argument('--screenshot',type=Path)
    p.add_argument('--at',type=float,default=0.)
    args=p.parse_args()
    # The development workspace already bundles this optional Qt dependency.
    # Load only the missing library, avoiding a broad sysroot LD_LIBRARY_PATH.
    import ctypes,ctypes.util
    cursor=ROOT/'.build-cache/self-contained/sysroot/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0.0.0'
    if sys.platform=='linux' and not ctypes.util.find_library('xcb-cursor') and cursor.is_file():
        ctypes.CDLL(str(cursor),mode=ctypes.RTLD_GLOBAL)
    app=QGuiApplication([]);scene=RobotScene(ROOT);replay=Replay(args.results,scene)
    engine=QQmlApplicationEngine()
    engine.rootContext().setContextProperty('backend',replay)
    engine.rootContext().setContextProperty('systemDark',False)
    engine.rootContext().setContextProperty('robotScene',scene)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name('Viewer.qml'))))
    if not engine.rootObjects():return 1
    if args.screenshot:
        def capture():
            replay.seek(args.at)
            def save():
                args.screenshot.parent.mkdir(parents=True,exist_ok=True)
                ok=engine.rootObjects()[0].grabWindow().save(str(args.screenshot))
                app.exit(0 if ok else 1)
            QTimer.singleShot(400,save)
        QTimer.singleShot(800,capture)
    code=app.exec();replay.timer.stop()
    return code


if __name__=='__main__':raise SystemExit(main())
