import collections,json,socket,struct,sys
from pathlib import Path

def parse(path):
    with Path(path).open('rb') as f:
        header=f.read(24)
        endian='<' if header[:4]==b'\xd4\xc3\xb2\xa1' else '>'
        link=struct.unpack(endian+'I',header[20:24])[0]
        offset={113:16,1:14}[link]
        while h:=f.read(16):
            sec,usec,n,original=struct.unpack(endian+'4I',h)
            b=f.read(n);ip=b[offset:]
            if not ip or ip[0]>>4!=4:continue
            ihl=(ip[0]&15)*4
            if ip[9]!=17:continue
            udp=ip[ihl:];sp,dp,length,checksum=struct.unpack('!4H',udp[:8]);data=udp[8:length]
            if len(data)<18 or data[:2]!=b'CH':continue
            ch,sub,serial,size=struct.unpack('<4i',data[2:18]);payload=data[18:18+size]
            yield {'ts':sec+usec/1e6,'src':socket.inet_ntoa(ip[12:16]),'dst':socket.inet_ntoa(ip[16:20]),'sport':sp,'dport':dp,
                'ch':ch,'sub':sub,'serial':serial,'size':size,'payload':payload.hex(),'udp_payload_length':len(data)}
if __name__=='__main__':
    rows=list(parse(sys.argv[1]));Path(sys.argv[1]+'.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    counts=collections.Counter((r['src'],r['dst'],r['ch'],r['payload']) for r in rows)
    print(json.dumps({'packets':len(rows),'payload_counts':[{ 'source':k[0],'destination':k[1],'arm_ch':k[2],'hex':k[3],'count':v} for k,v in counts.most_common(20)]},indent=2))
