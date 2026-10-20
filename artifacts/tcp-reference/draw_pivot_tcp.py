from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
import math
out=Path(__file__).resolve().parent
im=Image.new('RGB',(1400,1000),'#f4f7fb');d=ImageDraw.Draw(im)
font='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
def text(p,s,n=26,c='#26374b'): d.text(p,s,font=ImageFont.truetype(font,n),fill=c)
def line(p,c='#7c8fa5',w=4):d.line(p,fill=c,width=w)
def dash(a,b,c='#7c8fa5',w=3):
 length=math.dist(a,b)
 for t in range(0,int(length),18):
  p=lambda v:(a[0]+(b[0]-a[0])*v/length,a[1]+(b[1]-a[1])*v/length)
  line([p(t),p(min(t+10,length))],c,w)
def dot(p,c,r=12):d.ellipse((p[0]-r,p[1]-r,p[0]+r,p[1]+r),fill=c,outline='white',width=3)
def arrow(a,b,c='#7c8fa5',both=False):
 line([a,b],c,3)
 def head(x,y):
  q=math.atan2(y[1]-x[1],y[0]-x[0]);r=14
  d.polygon([y,(y[0]-r*math.cos(q-.45),y[1]-r*math.sin(q-.45)),(y[0]-r*math.cos(q+.45),y[1]-r*math.sin(q+.45))],fill=c)
 head(a,b)
 if both:head(b,a)
blue='#176bcb';green='#328c73';orange='#cc701f'
text((50,30),'修正理解：控制点放在两侧夹指的固定转轴高度',37)
text((50,90),'正视投影示意 · A / B 是固定两侧夹指的转轴 · 电机轴线用竖直虚线表示',23,'#64748b')
d.rounded_rectangle((35,140,1365,825),22,fill='white',outline='#d9e2ec',width=2)
# motor and mounting flange
for box in ((430,568,670,735),(393,748,707,784)):
 d.rounded_rectangle(box,12,fill='#dce4ee',outline='#7c8fa5',width=3)
text((480,637),'夹爪电机',27)
# fixed support and two arms
line([(440,578),(380,520),(350,460),(400,345),(410,270)],green,15)
line([(660,578),(720,520),(750,460),(700,345),(690,270)],green,15)
for x in (410,690):d.rounded_rectangle((x-22,225,x+22,300),6,fill='#cceadd',outline=green,width=3)
text((340,174),'左侧夹指',23,green);text((647,174),'右侧夹指',23,green)
# current point
line([(410,262),(690,262)],'#d4bba4',2)
dot((578,262),orange,10)
text((746,222),'M 当前控制点',25,orange)
text((746,260),'位于指垫区域，约 z = 202.453 mm',22,orange)
line([(593,262),(730,247)],orange,2)
# motor axis
arrow((550,727),(550,195),'#8091a6');dash((550,790),(550,195),'#8091a6')
# support axle line
line([(350,460),(750,460)],blue,5)
for x in (350,750):
 d.ellipse((x-22,438,x+22,482),fill='white',outline=green,width=5)
 dot((x,460),green,6)
dot((550,460),blue,15)
text((294,483),'A',27,green);text((770,483),'B',27,green)
text((202,393),'固定左夹指的轴',23,green)
text((709,393),'固定右夹指的轴',23,green)
text((579,486),'P 你指的位置',27,blue)
text((865,442),'两固定转轴所在截面',25,blue)
text((865,480),'与电机轴线的交点',25,blue)
line([(760,460),(842,460)],blue,3)
text((875,565),'电机轴线',22,'#64748b')
line([(690,602),(860,580)],'#7c8fa5',2)
line([(690,602),(552,602)],'#7c8fa5',2)
# dimension
line([(180,460),(325,460)],'#aab8c8',2);line([(180,765),(390,765)],'#aab8c8',2)
arrow((198,460),(198,765),both=True)
text((73,589),'约 142.23',24);text((115,626),'mm',23)
text((382,787),'法兰 / 夹爪根部 z = 0',21)
# displacement
arrow((1118,320),(1118,406),orange)
text((885,331),'比当前点向电机靠近约 60.23 mm',21,orange)
d.rounded_rectangle((35,850,1365,972),18,fill='#e6effa')
text((60,864),'反馈重点：蓝色 P 是否就是你说的“固定左右夹爪的轴的连线与电机轴相交的点”？',25,blue)
text((60,907),'URDF 两转轴的原点不在电机轴同一正视截面；图按两转轴所在平面画投影，尺寸为 CAD 名义值。',21,'#52657a')
text((60,940),'该点在夹指根部，随整个夹爪运动，但不随夹指开合改变。',21,'#52657a')
im.save(out/'tcp-pivot-reference.png')
print(out/'tcp-pivot-reference.png')
