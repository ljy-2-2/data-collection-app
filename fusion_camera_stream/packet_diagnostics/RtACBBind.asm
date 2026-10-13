
fusion_camera_stream/packet_diagnostics/libraries/libFXRtShm.so：     文件格式 elf32-i386


Disassembly of section .text:

00000f06 <RtACBBind(void*, char*)>:
     f06:	55                   	push   %ebp
     f07:	89 e5                	mov    %esp,%ebp
     f09:	53                   	push   %ebx
     f0a:	83 ec 34             	sub    $0x34,%esp
     f0d:	e8 be fa ff ff       	call   9d0 <__x86.get_pc_thunk.bx>
     f12:	81 c3 ee 30 00 00    	add    $0x30ee,%ebx
     f18:	8b 45 08             	mov    0x8(%ebp),%eax
     f1b:	89 45 f4             	mov    %eax,-0xc(%ebp)
     f1e:	8b 45 f4             	mov    -0xc(%ebp),%eax
     f21:	8b 55 0c             	mov    0xc(%ebp),%edx
     f24:	89 54 24 04          	mov    %edx,0x4(%esp)
     f28:	89 04 24             	mov    %eax,(%esp)
     f2b:	e8 10 fa ff ff       	call   940 <strcpy@plt>
     f30:	8b 45 f4             	mov    -0xc(%ebp),%eax
     f33:	c7 40 34 00 00 00 00 	movl   $0x0,0x34(%eax)
     f3a:	8b 45 f4             	mov    -0xc(%ebp),%eax
     f3d:	8d 50 20             	lea    0x20(%eax),%edx
     f40:	c7 44 24 08 ff ff ff 	movl   $0xffffffff,0x8(%esp)
     f47:	ff 
     f48:	c7 44 24 0c ff ff ff 	movl   $0xffffffff,0xc(%esp)
     f4f:	ff 
     f50:	8b 45 0c             	mov    0xc(%ebp),%eax
     f53:	89 44 24 04          	mov    %eax,0x4(%esp)
     f57:	89 14 24             	mov    %edx,(%esp)
     f5a:	e8 31 fa ff ff       	call   990 <rt_heap_bind@plt>
     f5f:	c1 e8 1f             	shr    $0x1f,%eax
     f62:	84 c0                	test   %al,%al
     f64:	74 1f                	je     f85 <RtACBBind(void*, char*)+0x7f>
     f66:	8b 45 0c             	mov    0xc(%ebp),%eax
     f69:	89 44 24 04          	mov    %eax,0x4(%esp)
     f6d:	8d 83 7b dd ff ff    	lea    -0x2285(%ebx),%eax
     f73:	89 04 24             	mov    %eax,(%esp)
     f76:	e8 75 f9 ff ff       	call   8f0 <printf@plt>
     f7b:	b8 00 00 00 00       	mov    $0x0,%eax
     f80:	e9 cf 00 00 00       	jmp    1054 <RtACBBind(void*, char*)+0x14e>
     f85:	8b 45 f4             	mov    -0xc(%ebp),%eax
     f88:	8d 50 34             	lea    0x34(%eax),%edx
     f8b:	8b 45 f4             	mov    -0xc(%ebp),%eax
     f8e:	83 c0 20             	add    $0x20,%eax
     f91:	89 54 24 10          	mov    %edx,0x10(%esp)
     f95:	c7 44 24 08 ff ff ff 	movl   $0xffffffff,0x8(%esp)
     f9c:	ff 
     f9d:	c7 44 24 0c ff ff ff 	movl   $0xffffffff,0xc(%esp)
     fa4:	ff 
     fa5:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
     fac:	00 
     fad:	89 04 24             	mov    %eax,(%esp)
     fb0:	e8 4b f9 ff ff       	call   900 <rt_heap_alloc@plt>
     fb5:	c1 e8 1f             	shr    $0x1f,%eax
     fb8:	84 c0                	test   %al,%al
     fba:	74 1c                	je     fd8 <RtACBBind(void*, char*)+0xd2>
     fbc:	8b 45 0c             	mov    0xc(%ebp),%eax
     fbf:	89 44 24 04          	mov    %eax,0x4(%esp)
     fc3:	8d 83 8e dd ff ff    	lea    -0x2272(%ebx),%eax
     fc9:	89 04 24             	mov    %eax,(%esp)
     fcc:	e8 1f f9 ff ff       	call   8f0 <printf@plt>
     fd1:	b8 fe ff ff ff       	mov    $0xfffffffe,%eax
     fd6:	eb 7c                	jmp    1054 <RtACBBind(void*, char*)+0x14e>
     fd8:	8b 45 f4             	mov    -0xc(%ebp),%eax
     fdb:	8b 50 34             	mov    0x34(%eax),%edx
     fde:	8b 45 f4             	mov    -0xc(%ebp),%eax
     fe1:	89 50 38             	mov    %edx,0x38(%eax)
     fe4:	8b 45 f4             	mov    -0xc(%ebp),%eax
     fe7:	8b 40 34             	mov    0x34(%eax),%eax
     fea:	8d 50 04             	lea    0x4(%eax),%edx
     fed:	8b 45 f4             	mov    -0xc(%ebp),%eax
     ff0:	89 50 3c             	mov    %edx,0x3c(%eax)
     ff3:	8b 45 f4             	mov    -0xc(%ebp),%eax
     ff6:	8b 40 34             	mov    0x34(%eax),%eax
     ff9:	8d 50 05             	lea    0x5(%eax),%edx
     ffc:	8b 45 f4             	mov    -0xc(%ebp),%eax
     fff:	89 50 40             	mov    %edx,0x40(%eax)
    1002:	8b 45 f4             	mov    -0xc(%ebp),%eax
    1005:	8b 40 34             	mov    0x34(%eax),%eax
    1008:	8d 50 06             	lea    0x6(%eax),%edx
    100b:	8b 45 f4             	mov    -0xc(%ebp),%eax
    100e:	89 50 44             	mov    %edx,0x44(%eax)
    1011:	8b 45 f4             	mov    -0xc(%ebp),%eax
    1014:	8b 40 34             	mov    0x34(%eax),%eax
    1017:	8d 50 0a             	lea    0xa(%eax),%edx
    101a:	8b 45 f4             	mov    -0xc(%ebp),%eax
    101d:	89 50 48             	mov    %edx,0x48(%eax)
    1020:	8b 45 f4             	mov    -0xc(%ebp),%eax
    1023:	8b 40 34             	mov    0x34(%eax),%eax
    1026:	8d 50 0e             	lea    0xe(%eax),%edx
    1029:	8b 45 f4             	mov    -0xc(%ebp),%eax
    102c:	89 50 4c             	mov    %edx,0x4c(%eax)
    102f:	8b 45 f4             	mov    -0xc(%ebp),%eax
    1032:	8b 40 34             	mov    0x34(%eax),%eax
    1035:	8d 50 12             	lea    0x12(%eax),%edx
    1038:	8b 45 f4             	mov    -0xc(%ebp),%eax
    103b:	89 50 50             	mov    %edx,0x50(%eax)
    103e:	8b 45 f4             	mov    -0xc(%ebp),%eax
    1041:	8b 40 38             	mov    0x38(%eax),%eax
    1044:	8b 00                	mov    (%eax),%eax
    1046:	8d 50 ee             	lea    -0x12(%eax),%edx
    1049:	8b 45 f4             	mov    -0xc(%ebp),%eax
    104c:	89 50 54             	mov    %edx,0x54(%eax)
    104f:	b8 01 00 00 00       	mov    $0x1,%eax
    1054:	83 c4 34             	add    $0x34,%esp
    1057:	5b                   	pop    %ebx
    1058:	5d                   	pop    %ebp
    1059:	c3                   	ret    
