from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
import math
out=Path(__file__).resolve().parent
im=Image.new('RGB',(1600,1060),'#f3f6fa');d=ImageDraw.Draw(im)
font='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
def text(pos,s,size=25,color='#243449'):
 d.text(pos,s,font=ImageFont.truetype(font,size),fill=color)
def line(points,color='#597083',width=4):d.line(points,fill=color,width=width)
def dash(a,b,color='#8296aa',width=3):
 length=math.dist(a,b)
 for t in range(0,int(length),18):
  end=min(t+9,length)
  p=lambda v:(a[0]+(b[0]-a[0])*v/length,a[1]+(b[1]-a[1])*v/length)
  line([p(t),p(end)],color,width)
def arrow(a,b,color='#597083',width=3,both=False):
 line([a,b],color,width)
 def head(x,y):
  theta=math.atan2(y[1]-x[1],y[0]-x[0]);r=13
  d.polygon([y,(y[0]-r*math.cos(theta-.45),y[1]-r*math.sin(theta-.45)),(y[0]-r*math.cos(theta+.45),y[1]-r*math.sin(theta+.45))],fill=color)
 head(a,b)
 if both:head(b,a)
def dot(p,c,r=10):d.ellipse((p[0]-r,p[1]-r,p[0]+r,p[1]+r),fill=c,outline='white',width=3)
blue='#176bcb';orange='#d86a14';green='#3d947b'
text((55,30),'遥操作控制点：当前点 M → 希望改到的点 P',39)
text((58,90),'适用于每只夹爪 · 基于当前 URDF · 几何示意，非实物标定图',23,'#64748b')
for box in ((40,145,790,865),(815,145,1560,865)):
 d.rounded_rectangle(box,22,fill='white',outline='#dae2ec',width=2)
text((68,165),'① 侧视：先确认“横截面”的高度',28)
text((844,165),'② 沿电机轴看：确认横向控制点',28)
# Side view. Radial direction projected along the old offset.
axisx=395;plane=365;base=760;drive=549
# Motor housing and flange
for box in ((312,563,478,727),(280,736,510,773)):
 d.rounded_rectangle(box,9,fill='#dce4ee',outline='#8296aa',width=3)
text((323,635),'夹爪电机',23)
# fingers, approximate outlines only
line([(330,575),(274,515),(246,354),(246,306)],green,12)
line([(460,575),(550,515),(608,354),(608,306)],green,12)
d.rounded_rectangle((227,332,266,398),6,fill='#c9e9dc',outline=green,width=3)
d.rounded_rectangle((589,332,628,398),6,fill='#c9e9dc',outline=green,width=3)
# Cross section and output shaft
line([(180,plane),(676,plane)],blue,3)
text((172,241),'指垫中心所在横截面',25,blue)
text((172,277),'暂按 URDF 全开参考高度',21,'#64748b')
dash((axisx,792),(axisx,255),'#5c7084',3)
arrow((axisx,300),(axisx,245),'#5c7084')
text((414,448),'电机输出轴中心线',22)
dot((axisx,drive),'#5c7084',7)
text((482,543),'轴关节原点',19,'#64748b')
dot((axisx,plane),blue,12);dot((axisx+45,plane),orange,11)
text((349,394),'P',28,blue);text((445,394),'M',28,orange)
text((107,800),'指垫与连杆轮廓仅示意；左右安装旋转不改变轴上点。',20,'#64748b')
# dimensions in side view
line([(110,base),(280,base)],'#b1bdca',2)
line([(110,plane),(180,plane)],'#b1bdca',2)
arrow((127,base),(127,plane),'#5c7084',both=True)
text((66,470),'202.453',19);text((84,499),'mm',19)
text((270,775),'法兰 / 夹爪根部 z = 0',19)
arrow((704,drive),(704,plane),'#5c7084',both=True)
line([(axisx+10,drive),(720,drive)],'#b1bdca',2)
text((656,572),'94.053',19);text((681,600),'mm',19)
# Axial cross section, exact scaled coordinates for M
cx,cy=1185,525;scale=14
mx,my=cx-9.175*scale,cy-9.190*scale
d.ellipse((970,310,1400,740),outline='#dce7f1',width=3)
arrow((930,cy),(1455,cy),'#8fa0b1',2);arrow((cx,775),(cx,260),'#8fa0b1',2)
text((1430,540),'+X',22,'#64748b');text((1200,261),'+Y',22,'#64748b')
dash((mx,my),(mx,cy),'#d7b291',2);dash((mx,my),(cx,my),'#d7b291',2)
arrow((mx,my),(cx,cy),orange,4)
dot((mx,my),orange,13);dot((cx,cy),blue,14)
text((909,333),'M 当前 TCP',25,orange)
text((923,368),'两指垫中心的中点',21,orange)
text((1210,548),'P 目标 TCP',25,blue)
text((1210,583),'电机轴穿过截面处',21,blue)
text((1071,617),'偏移距离 ≈ 12.986 mm',22,orange)
text((873,707),'M = (−9.175, +9.190, 202.453) mm',22,orange)
text((873,748),'P = (0, 0, 202.453) mm',24,blue)
text((873,804),'坐标均相对各自夹爪根部 gripper_L / gripper_R。',20,'#64748b')
d.rounded_rectangle((40,890,1560,1028),18,fill='#e6effa')
text((65,907),'请反馈：蓝色 P 是否就是你想让手柄控制的位置？',27,blue)
text((65,950),'图中保留原来的轴向高度，只把控制点移到电机轴上；高度仍取全开参考值，不随夹爪开合移动。',23)
text((65,986),'如果要的是指尖平面、接触面或闭合时的位置，请指出①里横截面应该上移还是下移。',22,'#52657a')
im.save(out/'tcp-reference.png')
print(out/'tcp-reference.png')
