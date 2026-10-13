
fusion_camera_stream/packet_diagnostics/FXPSI.reference：     文件格式 elf32-i386


Disassembly of section .text:

0805deab <InitRtUDP(unsigned char*, TrajBuf*, TrajBuf*)>:
 805deab:	55                   	push   %ebp
 805deac:	89 e5                	mov    %esp,%ebp
 805deae:	83 ec 28             	sub    $0x28,%esp
 805deb1:	b8 96 05 00 00       	mov    $0x596,%eax
 805deb6:	89 44 24 08          	mov    %eax,0x8(%esp)
 805deba:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805dec1:	00 
 805dec2:	8b 45 08             	mov    0x8(%ebp),%eax
 805dec5:	89 04 24             	mov    %eax,(%esp)
 805dec8:	e8 d3 a8 ff ff       	call   80587a0 <memset@plt>
 805decd:	8b 45 08             	mov    0x8(%ebp),%eax
 805ded0:	c6 00 46             	movb   $0x46,(%eax)
 805ded3:	8b 45 08             	mov    0x8(%ebp),%eax
 805ded6:	83 c0 01             	add    $0x1,%eax
 805ded9:	c6 00 58             	movb   $0x58,(%eax)
 805dedc:	8b 45 08             	mov    0x8(%ebp),%eax
 805dedf:	a3 a0 68 09 08       	mov    %eax,0x80968a0
 805dee4:	8b 45 08             	mov    0x8(%ebp),%eax
 805dee7:	83 c0 02             	add    $0x2,%eax
 805deea:	a3 a4 68 09 08       	mov    %eax,0x80968a4
 805deef:	8b 45 0c             	mov    0xc(%ebp),%eax
 805def2:	a3 a8 68 09 08       	mov    %eax,0x80968a8
 805def7:	8b 45 10             	mov    0x10(%ebp),%eax
 805defa:	a3 ac 68 09 08       	mov    %eax,0x80968ac
 805deff:	c7 44 24 08 00 00 00 	movl   $0x0,0x8(%esp)
 805df06:	00 
 805df07:	c7 44 24 04 02 00 00 	movl   $0x2,0x4(%esp)
 805df0e:	00 
 805df0f:	c7 04 24 02 00 00 00 	movl   $0x2,(%esp)
 805df16:	e8 15 aa ff ff       	call   8058930 <socket@plt>
 805df1b:	a3 38 62 09 08       	mov    %eax,0x8096238
 805df20:	c7 44 24 08 10 00 00 	movl   $0x10,0x8(%esp)
 805df27:	00 
 805df28:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805df2f:	00 
 805df30:	c7 04 24 c4 62 09 08 	movl   $0x80962c4,(%esp)
 805df37:	e8 64 a8 ff ff       	call   80587a0 <memset@plt>
 805df3c:	c7 05 d4 62 09 08 10 	movl   $0x10,0x80962d4
 805df43:	00 00 00 
 805df46:	66 c7 05 c4 62 09 08 	movw   $0x2,0x80962c4
 805df4d:	02 00 
 805df4f:	c7 04 24 79 12 00 00 	movl   $0x1279,(%esp)
 805df56:	e8 b5 a8 ff ff       	call   8058810 <htons@plt>
 805df5b:	66 a3 c6 62 09 08    	mov    %ax,0x80962c6
 805df61:	c7 05 c8 62 09 08 00 	movl   $0x0,0x80962c8
 805df68:	00 00 00 
 805df6b:	c7 44 24 08 00 00 00 	movl   $0x0,0x8(%esp)
 805df72:	00 
 805df73:	c7 44 24 04 02 00 00 	movl   $0x2,0x4(%esp)
 805df7a:	00 
 805df7b:	c7 04 24 02 00 00 00 	movl   $0x2,(%esp)
 805df82:	e8 a9 a9 ff ff       	call   8058930 <socket@plt>
 805df87:	a3 3c 62 09 08       	mov    %eax,0x809623c
 805df8c:	a1 3c 62 09 08       	mov    0x809623c,%eax
 805df91:	c7 44 24 08 00 00 00 	movl   $0x0,0x8(%esp)
 805df98:	00 
 805df99:	c7 44 24 04 03 00 00 	movl   $0x3,0x4(%esp)
 805dfa0:	00 
 805dfa1:	89 04 24             	mov    %eax,(%esp)
 805dfa4:	e8 b7 ac ff ff       	call   8058c60 <fcntl@plt>
 805dfa9:	89 45 f4             	mov    %eax,-0xc(%ebp)
 805dfac:	83 7d f4 00          	cmpl   $0x0,-0xc(%ebp)
 805dfb0:	79 13                	jns    805dfc5 <InitRtUDP(unsigned char*, TrajBuf*, TrajBuf*)+0x11a>
 805dfb2:	c7 04 24 21 ae 08 08 	movl   $0x808ae21,(%esp)
 805dfb9:	e8 62 ac ff ff       	call   8058c20 <puts@plt>
 805dfbe:	b8 ff ff ff ff       	mov    $0xffffffff,%eax
 805dfc3:	eb 5d                	jmp    805e022 <InitRtUDP(unsigned char*, TrajBuf*, TrajBuf*)+0x177>
 805dfc5:	8b 45 f4             	mov    -0xc(%ebp),%eax
 805dfc8:	80 cc 08             	or     $0x8,%ah
 805dfcb:	89 c2                	mov    %eax,%edx
 805dfcd:	a1 3c 62 09 08       	mov    0x809623c,%eax
 805dfd2:	89 54 24 08          	mov    %edx,0x8(%esp)
 805dfd6:	c7 44 24 04 04 00 00 	movl   $0x4,0x4(%esp)
 805dfdd:	00 
 805dfde:	89 04 24             	mov    %eax,(%esp)
 805dfe1:	e8 7a ac ff ff       	call   8058c60 <fcntl@plt>
 805dfe6:	c1 e8 1f             	shr    $0x1f,%eax
 805dfe9:	84 c0                	test   %al,%al
 805dfeb:	74 13                	je     805e000 <InitRtUDP(unsigned char*, TrajBuf*, TrajBuf*)+0x155>
 805dfed:	c7 04 24 27 ae 08 08 	movl   $0x808ae27,(%esp)
 805dff4:	e8 27 ac ff ff       	call   8058c20 <puts@plt>
 805dff9:	b8 fe ff ff ff       	mov    $0xfffffffe,%eax
 805dffe:	eb 22                	jmp    805e022 <InitRtUDP(unsigned char*, TrajBuf*, TrajBuf*)+0x177>
 805e000:	a1 3c 62 09 08       	mov    0x809623c,%eax
 805e005:	c7 44 24 08 10 00 00 	movl   $0x10,0x8(%esp)
 805e00c:	00 
 805e00d:	c7 44 24 04 c4 62 09 	movl   $0x80962c4,0x4(%esp)
 805e014:	08 
 805e015:	89 04 24             	mov    %eax,(%esp)
 805e018:	e8 73 aa ff ff       	call   8058a90 <bind@plt>
 805e01d:	b8 00 00 00 00       	mov    $0x0,%eax
 805e022:	c9                   	leave  
 805e023:	c3                   	ret    

0805e024 <OpenDev(char const*)>:
 805e024:	55                   	push   %ebp
 805e025:	89 e5                	mov    %esp,%ebp
 805e027:	83 ec 28             	sub    $0x28,%esp
 805e02a:	c7 44 24 04 02 01 00 	movl   $0x102,0x4(%esp)
 805e031:	00 
 805e032:	8b 45 08             	mov    0x8(%ebp),%eax
 805e035:	89 04 24             	mov    %eax,(%esp)
 805e038:	e8 a3 a5 ff ff       	call   80585e0 <open@plt>
 805e03d:	89 45 f4             	mov    %eax,-0xc(%ebp)
 805e040:	83 7d f4 ff          	cmpl   $0xffffffff,-0xc(%ebp)
 805e044:	75 13                	jne    805e059 <OpenDev(char const*)+0x35>
 805e046:	c7 04 24 50 ae 08 08 	movl   $0x808ae50,(%esp)
 805e04d:	e8 fe a7 ff ff       	call   8058850 <perror@plt>
 805e052:	b8 ff ff ff ff       	mov    $0xffffffff,%eax
 805e057:	eb 03                	jmp    805e05c <OpenDev(char const*)+0x38>
 805e059:	8b 45 f4             	mov    -0xc(%ebp),%eax
 805e05c:	c9                   	leave  
 805e05d:	c3                   	ret    

0805e05e <SetSpeed(int, int)>:
 805e05e:	55                   	push   %ebp
 805e05f:	89 e5                	mov    %esp,%ebp
 805e061:	81 ec b8 00 00 00    	sub    $0xb8,%esp
 805e067:	65 a1 14 00 00 00    	mov    %gs:0x14,%eax
 805e06d:	89 45 f4             	mov    %eax,-0xc(%ebp)
 805e070:	31 c0                	xor    %eax,%eax
 805e072:	c7 85 60 ff ff ff 04 	movl   $0x1004,-0xa0(%ebp)
 805e079:	10 00 00 
 805e07c:	c7 85 64 ff ff ff 03 	movl   $0x1003,-0x9c(%ebp)
 805e083:	10 00 00 
 805e086:	c7 85 68 ff ff ff 02 	movl   $0x1002,-0x98(%ebp)
 805e08d:	10 00 00 
 805e090:	c7 85 6c ff ff ff 01 	movl   $0x1001,-0x94(%ebp)
 805e097:	10 00 00 
 805e09a:	c7 85 70 ff ff ff 0f 	movl   $0xf,-0x90(%ebp)
 805e0a1:	00 00 00 
 805e0a4:	c7 85 74 ff ff ff 0e 	movl   $0xe,-0x8c(%ebp)
 805e0ab:	00 00 00 
 805e0ae:	c7 85 78 ff ff ff 0d 	movl   $0xd,-0x88(%ebp)
 805e0b5:	00 00 00 
 805e0b8:	c7 85 7c ff ff ff 0c 	movl   $0xc,-0x84(%ebp)
 805e0bf:	00 00 00 
 805e0c2:	c7 45 80 0b 00 00 00 	movl   $0xb,-0x80(%ebp)
 805e0c9:	c7 45 84 09 00 00 00 	movl   $0x9,-0x7c(%ebp)
 805e0d0:	c7 45 88 07 00 00 00 	movl   $0x7,-0x78(%ebp)
 805e0d7:	c7 45 8c 00 08 07 00 	movl   $0x70800,-0x74(%ebp)
 805e0de:	c7 45 90 00 84 03 00 	movl   $0x38400,-0x70(%ebp)
 805e0e5:	c7 45 94 00 c2 01 00 	movl   $0x1c200,-0x6c(%ebp)
 805e0ec:	c7 45 98 00 e1 00 00 	movl   $0xe100,-0x68(%ebp)
 805e0f3:	c7 45 9c 00 96 00 00 	movl   $0x9600,-0x64(%ebp)
 805e0fa:	c7 45 a0 00 4b 00 00 	movl   $0x4b00,-0x60(%ebp)
 805e101:	c7 45 a4 80 25 00 00 	movl   $0x2580,-0x5c(%ebp)
 805e108:	c7 45 a8 c0 12 00 00 	movl   $0x12c0,-0x58(%ebp)
 805e10f:	c7 45 ac 60 09 00 00 	movl   $0x960,-0x54(%ebp)
 805e116:	c7 45 b0 b0 04 00 00 	movl   $0x4b0,-0x50(%ebp)
 805e11d:	c7 45 b4 2c 01 00 00 	movl   $0x12c,-0x4c(%ebp)
 805e124:	8d 45 b8             	lea    -0x48(%ebp),%eax
 805e127:	89 44 24 04          	mov    %eax,0x4(%esp)
 805e12b:	8b 45 08             	mov    0x8(%ebp),%eax
 805e12e:	89 04 24             	mov    %eax,(%esp)
 805e131:	e8 ca a6 ff ff       	call   8058800 <tcgetattr@plt>
 805e136:	c7 85 58 ff ff ff 00 	movl   $0x0,-0xa8(%ebp)
 805e13d:	00 00 00 
 805e140:	e9 b4 00 00 00       	jmp    805e1f9 <SetSpeed(int, int)+0x19b>
 805e145:	8b 85 58 ff ff ff    	mov    -0xa8(%ebp),%eax
 805e14b:	8b 44 85 8c          	mov    -0x74(%ebp,%eax,4),%eax
 805e14f:	3b 45 0c             	cmp    0xc(%ebp),%eax
 805e152:	0f 85 9a 00 00 00    	jne    805e1f2 <SetSpeed(int, int)+0x194>
 805e158:	c7 44 24 04 02 00 00 	movl   $0x2,0x4(%esp)
 805e15f:	00 
 805e160:	8b 45 08             	mov    0x8(%ebp),%eax
 805e163:	89 04 24             	mov    %eax,(%esp)
 805e166:	e8 a5 a7 ff ff       	call   8058910 <tcflush@plt>
 805e16b:	8b 85 58 ff ff ff    	mov    -0xa8(%ebp),%eax
 805e171:	8b 84 85 60 ff ff ff 	mov    -0xa0(%ebp,%eax,4),%eax
 805e178:	89 44 24 04          	mov    %eax,0x4(%esp)
 805e17c:	8d 45 b8             	lea    -0x48(%ebp),%eax
 805e17f:	89 04 24             	mov    %eax,(%esp)
 805e182:	e8 39 ac ff ff       	call   8058dc0 <cfsetispeed@plt>
 805e187:	8b 85 58 ff ff ff    	mov    -0xa8(%ebp),%eax
 805e18d:	8b 84 85 60 ff ff ff 	mov    -0xa0(%ebp,%eax,4),%eax
 805e194:	89 44 24 04          	mov    %eax,0x4(%esp)
 805e198:	8d 45 b8             	lea    -0x48(%ebp),%eax
 805e19b:	89 04 24             	mov    %eax,(%esp)
 805e19e:	e8 1d a8 ff ff       	call   80589c0 <cfsetospeed@plt>
 805e1a3:	8d 45 b8             	lea    -0x48(%ebp),%eax
 805e1a6:	89 44 24 08          	mov    %eax,0x8(%esp)
 805e1aa:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805e1b1:	00 
 805e1b2:	8b 45 08             	mov    0x8(%ebp),%eax
 805e1b5:	89 04 24             	mov    %eax,(%esp)
 805e1b8:	e8 73 aa ff ff       	call   8058c30 <tcsetattr@plt>
 805e1bd:	89 85 5c ff ff ff    	mov    %eax,-0xa4(%ebp)
 805e1c3:	83 bd 5c ff ff ff 00 	cmpl   $0x0,-0xa4(%ebp)
 805e1ca:	74 13                	je     805e1df <SetSpeed(int, int)+0x181>
 805e1cc:	c7 04 24 67 ae 08 08 	movl   $0x808ae67,(%esp)
 805e1d3:	e8 78 a6 ff ff       	call   8058850 <perror@plt>
 805e1d8:	b8 00 00 00 00       	mov    $0x0,%eax
 805e1dd:	eb 32                	jmp    805e211 <SetSpeed(int, int)+0x1b3>
 805e1df:	c7 44 24 04 02 00 00 	movl   $0x2,0x4(%esp)
 805e1e6:	00 
 805e1e7:	8b 45 08             	mov    0x8(%ebp),%eax
 805e1ea:	89 04 24             	mov    %eax,(%esp)
 805e1ed:	e8 1e a7 ff ff       	call   8058910 <tcflush@plt>
 805e1f2:	83 85 58 ff ff ff 01 	addl   $0x1,-0xa8(%ebp)
 805e1f9:	8b 95 58 ff ff ff    	mov    -0xa8(%ebp),%edx
 805e1ff:	b8 0b 00 00 00       	mov    $0xb,%eax
 805e204:	39 c2                	cmp    %eax,%edx
 805e206:	0f 82 39 ff ff ff    	jb     805e145 <SetSpeed(int, int)+0xe7>
 805e20c:	b8 01 00 00 00       	mov    $0x1,%eax
 805e211:	8b 4d f4             	mov    -0xc(%ebp),%ecx
 805e214:	65 33 0d 14 00 00 00 	xor    %gs:0x14,%ecx
 805e21b:	74 05                	je     805e222 <SetSpeed(int, int)+0x1c4>
 805e21d:	e8 4e a9 ff ff       	call   8058b70 <__stack_chk_fail@plt>
 805e222:	c9                   	leave  
 805e223:	c3                   	ret    

0805e224 <SetParity(int, int, int, int)>:
 805e224:	55                   	push   %ebp
 805e225:	89 e5                	mov    %esp,%ebp
 805e227:	83 ec 58             	sub    $0x58,%esp
 805e22a:	65 a1 14 00 00 00    	mov    %gs:0x14,%eax
 805e230:	89 45 f4             	mov    %eax,-0xc(%ebp)
 805e233:	31 c0                	xor    %eax,%eax
 805e235:	8d 45 b8             	lea    -0x48(%ebp),%eax
 805e238:	89 44 24 04          	mov    %eax,0x4(%esp)
 805e23c:	8b 45 08             	mov    0x8(%ebp),%eax
 805e23f:	89 04 24             	mov    %eax,(%esp)
 805e242:	e8 b9 a5 ff ff       	call   8058800 <tcgetattr@plt>
 805e247:	85 c0                	test   %eax,%eax
 805e249:	0f 95 c0             	setne  %al
 805e24c:	84 c0                	test   %al,%al
 805e24e:	74 16                	je     805e266 <SetParity(int, int, int, int)+0x42>
 805e250:	c7 04 24 74 ae 08 08 	movl   $0x808ae74,(%esp)
 805e257:	e8 f4 a5 ff ff       	call   8058850 <perror@plt>
 805e25c:	b8 00 00 00 00       	mov    $0x0,%eax
 805e261:	e9 b0 01 00 00       	jmp    805e416 <SetParity(int, int, int, int)+0x1f2>
 805e266:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e269:	83 e0 cf             	and    $0xffffffcf,%eax
 805e26c:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e26f:	8b 45 0c             	mov    0xc(%ebp),%eax
 805e272:	83 f8 07             	cmp    $0x7,%eax
 805e275:	74 07                	je     805e27e <SetParity(int, int, int, int)+0x5a>
 805e277:	83 f8 08             	cmp    $0x8,%eax
 805e27a:	74 0d                	je     805e289 <SetParity(int, int, int, int)+0x65>
 805e27c:	eb 16                	jmp    805e294 <SetParity(int, int, int, int)+0x70>
 805e27e:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e281:	83 c8 20             	or     $0x20,%eax
 805e284:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e287:	eb 21                	jmp    805e2aa <SetParity(int, int, int, int)+0x86>
 805e289:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e28c:	83 c8 30             	or     $0x30,%eax
 805e28f:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e292:	eb 16                	jmp    805e2aa <SetParity(int, int, int, int)+0x86>
 805e294:	c7 04 24 81 ae 08 08 	movl   $0x808ae81,(%esp)
 805e29b:	e8 80 a9 ff ff       	call   8058c20 <puts@plt>
 805e2a0:	b8 00 00 00 00       	mov    $0x0,%eax
 805e2a5:	e9 6c 01 00 00       	jmp    805e416 <SetParity(int, int, int, int)+0x1f2>
 805e2aa:	8b 45 14             	mov    0x14(%ebp),%eax
 805e2ad:	83 e8 45             	sub    $0x45,%eax
 805e2b0:	83 f8 2e             	cmp    $0x2e,%eax
 805e2b3:	77 6b                	ja     805e320 <SetParity(int, int, int, int)+0xfc>
 805e2b5:	8b 04 85 c0 ae 08 08 	mov    0x808aec0(,%eax,4),%eax
 805e2bc:	ff e0                	jmp    *%eax
 805e2be:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e2c1:	80 e4 fe             	and    $0xfe,%ah
 805e2c4:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e2c7:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e2ca:	83 e0 ef             	and    $0xffffffef,%eax
 805e2cd:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e2d0:	eb 64                	jmp    805e336 <SetParity(int, int, int, int)+0x112>
 805e2d2:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e2d5:	80 cc 03             	or     $0x3,%ah
 805e2d8:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e2db:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e2de:	83 c8 10             	or     $0x10,%eax
 805e2e1:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e2e4:	eb 50                	jmp    805e336 <SetParity(int, int, int, int)+0x112>
 805e2e6:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e2e9:	80 cc 01             	or     $0x1,%ah
 805e2ec:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e2ef:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e2f2:	80 e4 fd             	and    $0xfd,%ah
 805e2f5:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e2f8:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e2fb:	83 c8 10             	or     $0x10,%eax
 805e2fe:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e301:	eb 33                	jmp    805e336 <SetParity(int, int, int, int)+0x112>
 805e303:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e306:	80 e4 fe             	and    $0xfe,%ah
 805e309:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e30c:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e30f:	83 e0 bf             	and    $0xffffffbf,%eax
 805e312:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e315:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e318:	83 c8 10             	or     $0x10,%eax
 805e31b:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e31e:	eb 16                	jmp    805e336 <SetParity(int, int, int, int)+0x112>
 805e320:	c7 04 24 97 ae 08 08 	movl   $0x808ae97,(%esp)
 805e327:	e8 f4 a8 ff ff       	call   8058c20 <puts@plt>
 805e32c:	b8 00 00 00 00       	mov    $0x0,%eax
 805e331:	e9 e0 00 00 00       	jmp    805e416 <SetParity(int, int, int, int)+0x1f2>
 805e336:	8b 45 10             	mov    0x10(%ebp),%eax
 805e339:	83 f8 01             	cmp    $0x1,%eax
 805e33c:	74 07                	je     805e345 <SetParity(int, int, int, int)+0x121>
 805e33e:	83 f8 02             	cmp    $0x2,%eax
 805e341:	74 0d                	je     805e350 <SetParity(int, int, int, int)+0x12c>
 805e343:	eb 16                	jmp    805e35b <SetParity(int, int, int, int)+0x137>
 805e345:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e348:	83 e0 bf             	and    $0xffffffbf,%eax
 805e34b:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e34e:	eb 21                	jmp    805e371 <SetParity(int, int, int, int)+0x14d>
 805e350:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e353:	83 c8 40             	or     $0x40,%eax
 805e356:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e359:	eb 16                	jmp    805e371 <SetParity(int, int, int, int)+0x14d>
 805e35b:	c7 04 24 aa ae 08 08 	movl   $0x808aeaa,(%esp)
 805e362:	e8 b9 a8 ff ff       	call   8058c20 <puts@plt>
 805e367:	b8 00 00 00 00       	mov    $0x0,%eax
 805e36c:	e9 a5 00 00 00       	jmp    805e416 <SetParity(int, int, int, int)+0x1f2>
 805e371:	83 7d 14 6e          	cmpl   $0x6e,0x14(%ebp)
 805e375:	75 06                	jne    805e37d <SetParity(int, int, int, int)+0x159>
 805e377:	83 7d 14 4e          	cmpl   $0x4e,0x14(%ebp)
 805e37b:	74 09                	je     805e386 <SetParity(int, int, int, int)+0x162>
 805e37d:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e380:	83 c8 10             	or     $0x10,%eax
 805e383:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e386:	8b 45 c0             	mov    -0x40(%ebp),%eax
 805e389:	0d 80 08 00 00       	or     $0x880,%eax
 805e38e:	89 45 c0             	mov    %eax,-0x40(%ebp)
 805e391:	8b 45 c4             	mov    -0x3c(%ebp),%eax
 805e394:	83 e0 e4             	and    $0xffffffe4,%eax
 805e397:	89 45 c4             	mov    %eax,-0x3c(%ebp)
 805e39a:	8b 45 bc             	mov    -0x44(%ebp),%eax
 805e39d:	83 e0 fe             	and    $0xfffffffe,%eax
 805e3a0:	89 45 bc             	mov    %eax,-0x44(%ebp)
 805e3a3:	8b 45 bc             	mov    -0x44(%ebp),%eax
 805e3a6:	83 e0 f3             	and    $0xfffffff3,%eax
 805e3a9:	89 45 bc             	mov    %eax,-0x44(%ebp)
 805e3ac:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e3af:	25 bf fe ff ff       	and    $0xfffffebf,%eax
 805e3b4:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e3b7:	8b 45 b8             	mov    -0x48(%ebp),%eax
 805e3ba:	80 e4 e3             	and    $0xe3,%ah
 805e3bd:	89 45 b8             	mov    %eax,-0x48(%ebp)
 805e3c0:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805e3c7:	00 
 805e3c8:	8b 45 08             	mov    0x8(%ebp),%eax
 805e3cb:	89 04 24             	mov    %eax,(%esp)
 805e3ce:	e8 3d a5 ff ff       	call   8058910 <tcflush@plt>
 805e3d3:	c6 45 ce 01          	movb   $0x1,-0x32(%ebp)
 805e3d7:	c6 45 cf 00          	movb   $0x0,-0x31(%ebp)
 805e3db:	8d 45 b8             	lea    -0x48(%ebp),%eax
 805e3de:	89 44 24 08          	mov    %eax,0x8(%esp)
 805e3e2:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805e3e9:	00 
 805e3ea:	8b 45 08             	mov    0x8(%ebp),%eax
 805e3ed:	89 04 24             	mov    %eax,(%esp)
 805e3f0:	e8 3b a8 ff ff       	call   8058c30 <tcsetattr@plt>
 805e3f5:	85 c0                	test   %eax,%eax
 805e3f7:	0f 95 c0             	setne  %al
 805e3fa:	84 c0                	test   %al,%al
 805e3fc:	74 13                	je     805e411 <SetParity(int, int, int, int)+0x1ed>
 805e3fe:	c7 04 24 67 ae 08 08 	movl   $0x808ae67,(%esp)
 805e405:	e8 46 a4 ff ff       	call   8058850 <perror@plt>
 805e40a:	b8 00 00 00 00       	mov    $0x0,%eax
 805e40f:	eb 05                	jmp    805e416 <SetParity(int, int, int, int)+0x1f2>
 805e411:	b8 01 00 00 00       	mov    $0x1,%eax
 805e416:	8b 55 f4             	mov    -0xc(%ebp),%edx
 805e419:	65 33 15 14 00 00 00 	xor    %gs:0x14,%edx
 805e420:	74 05                	je     805e427 <SetParity(int, int, int, int)+0x203>
 805e422:	e8 49 a7 ff ff       	call   8058b70 <__stack_chk_fail@plt>
 805e427:	c9                   	leave  
 805e428:	c3                   	ret    
 805e429:	90                   	nop

0805e42a <SerialPort::SerialPort()>:
 805e42a:	55                   	push   %ebp
 805e42b:	89 e5                	mov    %esp,%ebp
 805e42d:	8b 45 08             	mov    0x8(%ebp),%eax
 805e430:	c7 00 00 b0 08 08    	movl   $0x808b000,(%eax)
 805e436:	8b 45 08             	mov    0x8(%ebp),%eax
 805e439:	c7 40 04 ff ff ff ff 	movl   $0xffffffff,0x4(%eax)
 805e440:	8b 45 08             	mov    0x8(%ebp),%eax
 805e443:	c6 40 08 00          	movb   $0x0,0x8(%eax)
 805e447:	5d                   	pop    %ebp
 805e448:	c3                   	ret    
 805e449:	90                   	nop

0805e44a <SerialPort::~SerialPort()>:
 805e44a:	55                   	push   %ebp
 805e44b:	89 e5                	mov    %esp,%ebp
 805e44d:	83 ec 18             	sub    $0x18,%esp
 805e450:	8b 45 08             	mov    0x8(%ebp),%eax
 805e453:	c7 00 00 b0 08 08    	movl   $0x808b000,(%eax)
 805e459:	8b 45 08             	mov    0x8(%ebp),%eax
 805e45c:	89 04 24             	mov    %eax,(%esp)
 805e45f:	e8 b4 01 00 00       	call   805e618 <SerialPort::Close()>
 805e464:	b8 00 00 00 00       	mov    $0x0,%eax
 805e469:	85 c0                	test   %eax,%eax
 805e46b:	74 0b                	je     805e478 <SerialPort::~SerialPort()+0x2e>
 805e46d:	8b 45 08             	mov    0x8(%ebp),%eax
 805e470:	89 04 24             	mov    %eax,(%esp)
 805e473:	e8 48 a2 ff ff       	call   80586c0 <operator delete(void*)@plt>
 805e478:	c9                   	leave  
 805e479:	c3                   	ret    

0805e47a <SerialPort::~SerialPort()>:
 805e47a:	55                   	push   %ebp
 805e47b:	89 e5                	mov    %esp,%ebp
 805e47d:	83 ec 18             	sub    $0x18,%esp
 805e480:	8b 45 08             	mov    0x8(%ebp),%eax
 805e483:	89 04 24             	mov    %eax,(%esp)
 805e486:	e8 bf ff ff ff       	call   805e44a <SerialPort::~SerialPort()>
 805e48b:	8b 45 08             	mov    0x8(%ebp),%eax
 805e48e:	89 04 24             	mov    %eax,(%esp)
 805e491:	e8 2a a2 ff ff       	call   80586c0 <operator delete(void*)@plt>
 805e496:	c9                   	leave  
 805e497:	c3                   	ret    

0805e498 <SerialPort::Connect(char const*, int, int, int, int)>:
 805e498:	55                   	push   %ebp
 805e499:	89 e5                	mov    %esp,%ebp
 805e49b:	83 ec 48             	sub    $0x48,%esp
 805e49e:	8b 45 08             	mov    0x8(%ebp),%eax
 805e4a1:	89 45 d4             	mov    %eax,-0x2c(%ebp)
 805e4a4:	8b 45 0c             	mov    0xc(%ebp),%eax
 805e4a7:	89 45 d0             	mov    %eax,-0x30(%ebp)
 805e4aa:	65 a1 14 00 00 00    	mov    %gs:0x14,%eax
 805e4b0:	89 45 f4             	mov    %eax,-0xc(%ebp)
 805e4b3:	31 c0                	xor    %eax,%eax
 805e4b5:	83 7d d0 00          	cmpl   $0x0,-0x30(%ebp)
 805e4b9:	74 0a                	je     805e4c5 <SerialPort::Connect(char const*, int, int, int, int)+0x2d>
 805e4bb:	8b 45 d0             	mov    -0x30(%ebp),%eax
 805e4be:	0f b6 00             	movzbl (%eax),%eax
 805e4c1:	84 c0                	test   %al,%al
 805e4c3:	75 16                	jne    805e4db <SerialPort::Connect(char const*, int, int, int, int)+0x43>
 805e4c5:	c7 04 24 7c af 08 08 	movl   $0x808af7c,(%esp)
 805e4cc:	e8 4f a7 ff ff       	call   8058c20 <puts@plt>
 805e4d1:	b8 00 00 00 00       	mov    $0x0,%eax
 805e4d6:	e9 29 01 00 00       	jmp    805e604 <SerialPort::Connect(char const*, int, int, int, int)+0x16c>
 805e4db:	83 7d 10 00          	cmpl   $0x0,0x10(%ebp)
 805e4df:	75 16                	jne    805e4f7 <SerialPort::Connect(char const*, int, int, int, int)+0x5f>
 805e4e1:	c7 04 24 98 af 08 08 	movl   $0x808af98,(%esp)
 805e4e8:	e8 33 a7 ff ff       	call   8058c20 <puts@plt>
 805e4ed:	b8 00 00 00 00       	mov    $0x0,%eax
 805e4f2:	e9 0d 01 00 00       	jmp    805e604 <SerialPort::Connect(char const*, int, int, int, int)+0x16c>
 805e4f7:	8b 45 d0             	mov    -0x30(%ebp),%eax
 805e4fa:	89 04 24             	mov    %eax,(%esp)
 805e4fd:	e8                   	.byte 0xe8
 805e4fe:	22                   	.byte 0x22
