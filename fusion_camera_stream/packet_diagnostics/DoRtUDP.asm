
fusion_camera_stream/packet_diagnostics/FXPSI.reference：     文件格式 elf32-i386


Disassembly of section .text:

0805a9cc <DoRtUDP()>:
 805a9cc:	55                   	push   %ebp
 805a9cd:	89 e5                	mov    %esp,%ebp
 805a9cf:	56                   	push   %esi
 805a9d0:	53                   	push   %ebx
 805a9d1:	81 ec 40 04 00 00    	sub    $0x440,%esp
 805a9d7:	65 a1 14 00 00 00    	mov    %gs:0x14,%eax
 805a9dd:	89 45 f4             	mov    %eax,-0xc(%ebp)
 805a9e0:	31 c0                	xor    %eax,%eax
 805a9e2:	c7 85 e8 fb ff ff 00 	movl   $0x0,-0x418(%ebp)
 805a9e9:	00 00 00 
 805a9ec:	a1 d8 62 09 08       	mov    0x80962d8,%eax
 805a9f1:	83 f8 01             	cmp    $0x1,%eax
 805a9f4:	75 0a                	jne    805aa00 <DoRtUDP()+0x34>
 805a9f6:	c7 85 e8 fb ff ff 01 	movl   $0x1,-0x418(%ebp)
 805a9fd:	00 00 00 
 805aa00:	c7 05 b0 68 09 08 00 	movl   $0x0,0x80968b0
 805aa07:	00 00 00 
 805aa0a:	c7 85 0c fc ff ff 00 	movl   $0x0,-0x3f4(%ebp)
 805aa11:	00 00 00 
 805aa14:	c7 05 c0 62 09 08 10 	movl   $0x10,0x80962c0
 805aa1b:	00 00 00 
 805aa1e:	a1 3c 62 09 08       	mov    0x809623c,%eax
 805aa23:	c7 44 24 14 c0 62 09 	movl   $0x80962c0,0x14(%esp)
 805aa2a:	08 
 805aa2b:	c7 44 24 10 b0 62 09 	movl   $0x80962b0,0x10(%esp)
 805aa32:	08 
 805aa33:	c7 44 24 0c 00 00 00 	movl   $0x0,0xc(%esp)
 805aa3a:	00 
 805aa3b:	c7 44 24 08 c0 05 00 	movl   $0x5c0,0x8(%esp)
 805aa42:	00 
 805aa43:	c7 44 24 04 e0 62 09 	movl   $0x80962e0,0x4(%esp)
 805aa4a:	08 
 805aa4b:	89 04 24             	mov    %eax,(%esp)
 805aa4e:	e8 0d e3 ff ff       	call   8058d60 <recvfrom@plt>
 805aa53:	89 85 10 fc ff ff    	mov    %eax,-0x3f0(%ebp)
 805aa59:	c7 85 ec fb ff ff 00 	movl   $0x0,-0x414(%ebp)
 805aa60:	00 00 00 
 805aa63:	83 bd 10 fc ff ff ff 	cmpl   $0xffffffff,-0x3f0(%ebp)
 805aa6a:	0f 84 e5 2f 00 00    	je     805da55 <DoRtUDP()+0x3089>
 805aa70:	83 bd 10 fc ff ff 00 	cmpl   $0x0,-0x3f0(%ebp)
 805aa77:	0f 8e d8 2f 00 00    	jle    805da55 <DoRtUDP()+0x3089>
 805aa7d:	0f b6 05 e0 62 09 08 	movzbl 0x80962e0,%eax
 805aa84:	3c 46                	cmp    $0x46,%al
 805aa86:	75 17                	jne    805aa9f <DoRtUDP()+0xd3>
 805aa88:	0f b6 05 e1 62 09 08 	movzbl 0x80962e1,%eax
 805aa8f:	3c 58                	cmp    $0x58,%al
 805aa91:	75 0c                	jne    805aa9f <DoRtUDP()+0xd3>
 805aa93:	c7 85 ec fb ff ff 01 	movl   $0x1,-0x414(%ebp)
 805aa9a:	00 00 00 
 805aa9d:	eb 20                	jmp    805aabf <DoRtUDP()+0xf3>
 805aa9f:	0f b6 05 e0 62 09 08 	movzbl 0x80962e0,%eax
 805aaa6:	3c 46                	cmp    $0x46,%al
 805aaa8:	75 15                	jne    805aabf <DoRtUDP()+0xf3>
 805aaaa:	0f b6 05 e1 62 09 08 	movzbl 0x80962e1,%eax
 805aab1:	3c 59                	cmp    $0x59,%al
 805aab3:	75 0a                	jne    805aabf <DoRtUDP()+0xf3>
 805aab5:	c7 85 ec fb ff ff 02 	movl   $0x2,-0x414(%ebp)
 805aabc:	00 00 00 
 805aabf:	83 bd ec fb ff ff 00 	cmpl   $0x0,-0x414(%ebp)
 805aac6:	0f 85 50 01 00 00    	jne    805ac1c <DoRtUDP()+0x250>
 805aacc:	0f b6 05 e0 62 09 08 	movzbl 0x80962e0,%eax
 805aad3:	3c 43                	cmp    $0x43,%al
 805aad5:	0f 85 3c 01 00 00    	jne    805ac17 <DoRtUDP()+0x24b>
 805aadb:	0f b6 05 e1 62 09 08 	movzbl 0x80962e1,%eax
 805aae2:	3c 48                	cmp    $0x48,%al
 805aae4:	0f 85 2d 01 00 00    	jne    805ac17 <DoRtUDP()+0x24b>
 805aaea:	8b 95 10 fc ff ff    	mov    -0x3f0(%ebp),%edx
 805aaf0:	b8 12 01 00 00       	mov    $0x112,%eax
 805aaf5:	39 c2                	cmp    %eax,%edx
 805aaf7:	0f 85 1a 01 00 00    	jne    805ac17 <DoRtUDP()+0x24b>
 805aafd:	c7 85 14 fc ff ff e2 	movl   $0x80962e2,-0x3ec(%ebp)
 805ab04:	62 09 08 
 805ab07:	8b 85 14 fc ff ff    	mov    -0x3ec(%ebp),%eax
 805ab0d:	8b 40 0c             	mov    0xc(%eax),%eax
 805ab10:	89 85 18 fc ff ff    	mov    %eax,-0x3e8(%ebp)
 805ab16:	8b 85 18 fc ff ff    	mov    -0x3e8(%ebp),%eax
 805ab1c:	8b 95 14 fc ff ff    	mov    -0x3ec(%ebp),%edx
 805ab22:	83 c2 10             	add    $0x10,%edx
 805ab25:	89 44 24 08          	mov    %eax,0x8(%esp)
 805ab29:	89 54 24 04          	mov    %edx,0x4(%esp)
 805ab2d:	8d 85 00 fe ff ff    	lea    -0x200(%ebp),%eax
 805ab33:	83 c0 01             	add    $0x1,%eax
 805ab36:	89 04 24             	mov    %eax,(%esp)
 805ab39:	e8 72 de ff ff       	call   80589b0 <memcpy@plt>
 805ab3e:	c6 85 00 fe ff ff 00 	movb   $0x0,-0x200(%ebp)
 805ab45:	8b 85 14 fc ff ff    	mov    -0x3ec(%ebp),%eax
 805ab4b:	8b 40 04             	mov    0x4(%eax),%eax
 805ab4e:	83 f8 01             	cmp    $0x1,%eax
 805ab51:	75 09                	jne    805ab5c <DoRtUDP()+0x190>
 805ab53:	c6 85 00 fe ff ff 43 	movb   $0x43,-0x200(%ebp)
 805ab5a:	eb 2c                	jmp    805ab88 <DoRtUDP()+0x1bc>
 805ab5c:	8b 85 14 fc ff ff    	mov    -0x3ec(%ebp),%eax
 805ab62:	8b 40 04             	mov    0x4(%eax),%eax
 805ab65:	83 f8 02             	cmp    $0x2,%eax
 805ab68:	75 09                	jne    805ab73 <DoRtUDP()+0x1a7>
 805ab6a:	c6 85 00 fe ff ff 41 	movb   $0x41,-0x200(%ebp)
 805ab71:	eb 15                	jmp    805ab88 <DoRtUDP()+0x1bc>
 805ab73:	8b 85 14 fc ff ff    	mov    -0x3ec(%ebp),%eax
 805ab79:	8b 40 04             	mov    0x4(%eax),%eax
 805ab7c:	83 f8 03             	cmp    $0x3,%eax
 805ab7f:	75 07                	jne    805ab88 <DoRtUDP()+0x1bc>
 805ab81:	c6 85 00 fe ff ff 42 	movb   $0x42,-0x200(%ebp)
 805ab88:	0f b6 85 00 fe ff ff 	movzbl -0x200(%ebp),%eax
 805ab8f:	84 c0                	test   %al,%al
 805ab91:	0f 84 80 00 00 00    	je     805ac17 <DoRtUDP()+0x24b>
 805ab97:	8b 85 14 fc ff ff    	mov    -0x3ec(%ebp),%eax
 805ab9d:	8b 00                	mov    (%eax),%eax
 805ab9f:	83 f8 01             	cmp    $0x1,%eax
 805aba2:	75 33                	jne    805abd7 <DoRtUDP()+0x20b>
 805aba4:	a1 a8 62 09 08       	mov    0x80962a8,%eax
 805aba9:	85 c0                	test   %eax,%eax
 805abab:	74 2a                	je     805abd7 <DoRtUDP()+0x20b>
 805abad:	a1 a8 62 09 08       	mov    0x80962a8,%eax
 805abb2:	8b 40 5c             	mov    0x5c(%eax),%eax
 805abb5:	8b 95 18 fc ff ff    	mov    -0x3e8(%ebp),%edx
 805abbb:	8d 4a 01             	lea    0x1(%edx),%ecx
 805abbe:	8b 15 a8 62 09 08    	mov    0x80962a8,%edx
 805abc4:	89 4c 24 08          	mov    %ecx,0x8(%esp)
 805abc8:	8d 8d 00 fe ff ff    	lea    -0x200(%ebp),%ecx
 805abce:	89 4c 24 04          	mov    %ecx,0x4(%esp)
 805abd2:	89 14 24             	mov    %edx,(%esp)
 805abd5:	ff d0                	call   *%eax
 805abd7:	8b 85 14 fc ff ff    	mov    -0x3ec(%ebp),%eax
 805abdd:	8b 00                	mov    (%eax),%eax
 805abdf:	83 f8 02             	cmp    $0x2,%eax
 805abe2:	75 33                	jne    805ac17 <DoRtUDP()+0x24b>
 805abe4:	a1 ac 62 09 08       	mov    0x80962ac,%eax
 805abe9:	85 c0                	test   %eax,%eax
 805abeb:	74 2a                	je     805ac17 <DoRtUDP()+0x24b>
 805abed:	a1 ac 62 09 08       	mov    0x80962ac,%eax
 805abf2:	8b 40 5c             	mov    0x5c(%eax),%eax
 805abf5:	8b 95 18 fc ff ff    	mov    -0x3e8(%ebp),%edx
 805abfb:	8d 4a 01             	lea    0x1(%edx),%ecx
 805abfe:	8b 15 ac 62 09 08    	mov    0x80962ac,%edx
 805ac04:	89 4c 24 08          	mov    %ecx,0x8(%esp)
 805ac08:	8d 8d 00 fe ff ff    	lea    -0x200(%ebp),%ecx
 805ac0e:	89 4c 24 04          	mov    %ecx,0x4(%esp)
 805ac12:	89 14 24             	mov    %edx,(%esp)
 805ac15:	ff d0                	call   *%eax
 805ac17:	e9 39 2e 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805ac1c:	83 bd ec fb ff ff 01 	cmpl   $0x1,-0x414(%ebp)
 805ac23:	74 0d                	je     805ac32 <DoRtUDP()+0x266>
 805ac25:	83 bd ec fb ff ff 02 	cmpl   $0x2,-0x414(%ebp)
 805ac2c:	0f 85 23 2e 00 00    	jne    805da55 <DoRtUDP()+0x3089>
 805ac32:	c7 85 f0 fb ff ff 00 	movl   $0x0,-0x410(%ebp)
 805ac39:	00 00 00 
 805ac3c:	83 bd ec fb ff ff 01 	cmpl   $0x1,-0x414(%ebp)
 805ac43:	75 0a                	jne    805ac4f <DoRtUDP()+0x283>
 805ac45:	c7 85 f0 fb ff ff 03 	movl   $0x3,-0x410(%ebp)
 805ac4c:	00 00 00 
 805ac4f:	83 bd ec fb ff ff 02 	cmpl   $0x2,-0x414(%ebp)
 805ac56:	0f 85 a3 01 00 00    	jne    805adff <DoRtUDP()+0x433>
 805ac5c:	c6 85 e2 fb ff ff 00 	movb   $0x0,-0x41e(%ebp)
 805ac63:	0f b6 05 e0 62 09 08 	movzbl 0x80962e0,%eax
 805ac6a:	00 85 e2 fb ff ff    	add    %al,-0x41e(%ebp)
 805ac70:	0f b6 05 e1 62 09 08 	movzbl 0x80962e1,%eax
 805ac77:	00 85 e2 fb ff ff    	add    %al,-0x41e(%ebp)
 805ac7d:	0f b6 05 e2 62 09 08 	movzbl 0x80962e2,%eax
 805ac84:	00 85 e2 fb ff ff    	add    %al,-0x41e(%ebp)
 805ac8a:	0f b6 05 e3 62 09 08 	movzbl 0x80962e3,%eax
 805ac91:	00 85 e2 fb ff ff    	add    %al,-0x41e(%ebp)
 805ac97:	0f b6 05 e4 62 09 08 	movzbl 0x80962e4,%eax
 805ac9e:	00 85 e2 fb ff ff    	add    %al,-0x41e(%ebp)
 805aca4:	0f b6 05 e5 62 09 08 	movzbl 0x80962e5,%eax
 805acab:	00 85 e2 fb ff ff    	add    %al,-0x41e(%ebp)
 805acb1:	80 bd e2 fb ff ff 00 	cmpb   $0x0,-0x41e(%ebp)
 805acb8:	74 51                	je     805ad0b <DoRtUDP()+0x33f>
 805acba:	0f b6 05 e3 62 09 08 	movzbl 0x80962e3,%eax
 805acc1:	0f b6 d8             	movzbl %al,%ebx
 805acc4:	0f b6 05 e2 62 09 08 	movzbl 0x80962e2,%eax
 805accb:	0f b6 c8             	movzbl %al,%ecx
 805acce:	0f b6 05 e1 62 09 08 	movzbl 0x80962e1,%eax
 805acd5:	0f b6 d0             	movzbl %al,%edx
 805acd8:	0f b6 05 e0 62 09 08 	movzbl 0x80962e0,%eax
 805acdf:	0f b6 c0             	movzbl %al,%eax
 805ace2:	89 5c 24 14          	mov    %ebx,0x14(%esp)
 805ace6:	89 4c 24 10          	mov    %ecx,0x10(%esp)
 805acea:	89 54 24 0c          	mov    %edx,0xc(%esp)
 805acee:	89 44 24 08          	mov    %eax,0x8(%esp)
 805acf2:	c7 44 24 04 bc a7 08 	movl   $0x808a7bc,0x4(%esp)
 805acf9:	08 
 805acfa:	c7 04 24 a0 6f 09 08 	movl   $0x8096fa0,(%esp)
 805ad01:	e8 7c e7 ff ff       	call   8059482 <DebugLogClt::WARN_LOG(char const*, ...)>
 805ad06:	e9 4a 2d 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805ad0b:	0f b6 05 e3 62 09 08 	movzbl 0x80962e3,%eax
 805ad12:	0f b6 c0             	movzbl %al,%eax
 805ad15:	0f b6 15 e4 62 09 08 	movzbl 0x80962e4,%edx
 805ad1c:	0f b6 d2             	movzbl %dl,%edx
 805ad1f:	c1 e2 08             	shl    $0x8,%edx
 805ad22:	01 d0                	add    %edx,%eax
 805ad24:	89 85 1c fc ff ff    	mov    %eax,-0x3e4(%ebp)
 805ad2a:	8b 85 1c fc ff ff    	mov    -0x3e4(%ebp),%eax
 805ad30:	83 c0 07             	add    $0x7,%eax
 805ad33:	3b 85 10 fc ff ff    	cmp    -0x3f0(%ebp),%eax
 805ad39:	74 30                	je     805ad6b <DoRtUDP()+0x39f>
 805ad3b:	8b 85 1c fc ff ff    	mov    -0x3e4(%ebp),%eax
 805ad41:	83 c0 07             	add    $0x7,%eax
 805ad44:	89 44 24 0c          	mov    %eax,0xc(%esp)
 805ad48:	8b 85 10 fc ff ff    	mov    -0x3f0(%ebp),%eax
 805ad4e:	89 44 24 08          	mov    %eax,0x8(%esp)
 805ad52:	c7 44 24 04 e4 a7 08 	movl   $0x808a7e4,0x4(%esp)
 805ad59:	08 
 805ad5a:	c7 04 24 a0 6f 09 08 	movl   $0x8096fa0,(%esp)
 805ad61:	e8 1c e7 ff ff       	call   8059482 <DebugLogClt::WARN_LOG(char const*, ...)>
 805ad66:	e9 ea 2c 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805ad6b:	c6 85 e0 fb ff ff 00 	movb   $0x0,-0x420(%ebp)
 805ad72:	c7 85 f4 fb ff ff 07 	movl   $0x7,-0x40c(%ebp)
 805ad79:	00 00 00 
 805ad7c:	eb 28                	jmp    805ada6 <DoRtUDP()+0x3da>
 805ad7e:	8b 85 f4 fb ff ff    	mov    -0x40c(%ebp),%eax
 805ad84:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805ad89:	0f b6 00             	movzbl (%eax),%eax
 805ad8c:	88 85 e3 fb ff ff    	mov    %al,-0x41d(%ebp)
 805ad92:	0f b6 85 e3 fb ff ff 	movzbl -0x41d(%ebp),%eax
 805ad99:	00 85 e0 fb ff ff    	add    %al,-0x420(%ebp)
 805ad9f:	83 85 f4 fb ff ff 01 	addl   $0x1,-0x40c(%ebp)
 805ada6:	8b 85 f4 fb ff ff    	mov    -0x40c(%ebp),%eax
 805adac:	3b 85 10 fc ff ff    	cmp    -0x3f0(%ebp),%eax
 805adb2:	7c ca                	jl     805ad7e <DoRtUDP()+0x3b2>
 805adb4:	0f b6 05 e6 62 09 08 	movzbl 0x80962e6,%eax
 805adbb:	3a 85 e0 fb ff ff    	cmp    -0x420(%ebp),%al
 805adc1:	74 32                	je     805adf5 <DoRtUDP()+0x429>
 805adc3:	0f b6 05 e6 62 09 08 	movzbl 0x80962e6,%eax
 805adca:	0f b6 d0             	movzbl %al,%edx
 805adcd:	0f b6 85 e0 fb ff ff 	movzbl -0x420(%ebp),%eax
 805add4:	89 54 24 0c          	mov    %edx,0xc(%esp)
 805add8:	89 44 24 08          	mov    %eax,0x8(%esp)
 805addc:	c7 44 24 04 2c a8 08 	movl   $0x808a82c,0x4(%esp)
 805ade3:	08 
 805ade4:	c7 04 24 a0 6f 09 08 	movl   $0x8096fa0,(%esp)
 805adeb:	e8 92 e6 ff ff       	call   8059482 <DebugLogClt::WARN_LOG(char const*, ...)>
 805adf0:	e9 60 2c 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805adf5:	c7 85 f0 fb ff ff 07 	movl   $0x7,-0x410(%ebp)
 805adfc:	00 00 00 
 805adff:	c7 05 d8 62 09 08 01 	movl   $0x1,0x80962d8
 805ae06:	00 00 00 
 805ae09:	0f b6 05 e2 62 09 08 	movzbl 0x80962e2,%eax
 805ae10:	88 85 e4 fb ff ff    	mov    %al,-0x41c(%ebp)
 805ae16:	c7 85 f8 fb ff ff 00 	movl   $0x0,-0x408(%ebp)
 805ae1d:	00 00 00 
 805ae20:	e9 1d 2c 00 00       	jmp    805da42 <DoRtUDP()+0x3076>
 805ae25:	83 bd ec fb ff ff 02 	cmpl   $0x2,-0x414(%ebp)
 805ae2c:	0f 85 da 01 00 00    	jne    805b00c <DoRtUDP()+0x640>
 805ae32:	c6 85 e5 fb ff ff 00 	movb   $0x0,-0x41b(%ebp)
 805ae39:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ae3f:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805ae44:	0f b6 00             	movzbl (%eax),%eax
 805ae47:	00 85 e5 fb ff ff    	add    %al,-0x41b(%ebp)
 805ae4d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ae53:	83 c0 01             	add    $0x1,%eax
 805ae56:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805ae5d:	00 85 e5 fb ff ff    	add    %al,-0x41b(%ebp)
 805ae63:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ae69:	83 c0 02             	add    $0x2,%eax
 805ae6c:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805ae73:	00 85 e5 fb ff ff    	add    %al,-0x41b(%ebp)
 805ae79:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ae7f:	83 c0 03             	add    $0x3,%eax
 805ae82:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805ae89:	00 85 e5 fb ff ff    	add    %al,-0x41b(%ebp)
 805ae8f:	80 bd e5 fb ff ff 00 	cmpb   $0x0,-0x41b(%ebp)
 805ae96:	74 73                	je     805af0b <DoRtUDP()+0x53f>
 805ae98:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ae9e:	83 c0 03             	add    $0x3,%eax
 805aea1:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805aea8:	0f b6 d8             	movzbl %al,%ebx
 805aeab:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805aeb1:	83 c0 02             	add    $0x2,%eax
 805aeb4:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805aebb:	0f b6 c8             	movzbl %al,%ecx
 805aebe:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805aec4:	83 c0 01             	add    $0x1,%eax
 805aec7:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805aece:	0f b6 d0             	movzbl %al,%edx
 805aed1:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805aed7:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805aedc:	0f b6 00             	movzbl (%eax),%eax
 805aedf:	0f b6 c0             	movzbl %al,%eax
 805aee2:	89 5c 24 14          	mov    %ebx,0x14(%esp)
 805aee6:	89 4c 24 10          	mov    %ecx,0x10(%esp)
 805aeea:	89 54 24 0c          	mov    %edx,0xc(%esp)
 805aeee:	89 44 24 08          	mov    %eax,0x8(%esp)
 805aef2:	c7 44 24 04 4c a8 08 	movl   $0x808a84c,0x4(%esp)
 805aef9:	08 
 805aefa:	c7 04 24 a0 6f 09 08 	movl   $0x8096fa0,(%esp)
 805af01:	e8 7c e5 ff ff       	call   8059482 <DebugLogClt::WARN_LOG(char const*, ...)>
 805af06:	e9 4a 2b 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805af0b:	c6 85 e1 fb ff ff 00 	movb   $0x0,-0x41f(%ebp)
 805af12:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805af18:	83 c0 01             	add    $0x1,%eax
 805af1b:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805af22:	0f b6 c0             	movzbl %al,%eax
 805af25:	c1 e0 08             	shl    $0x8,%eax
 805af28:	89 c2                	mov    %eax,%edx
 805af2a:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805af30:	83 c0 02             	add    $0x2,%eax
 805af33:	0f b6 80 e0 62 09 08 	movzbl 0x80962e0(%eax),%eax
 805af3a:	0f b6 c0             	movzbl %al,%eax
 805af3d:	01 d0                	add    %edx,%eax
 805af3f:	89 85 20 fc ff ff    	mov    %eax,-0x3e0(%ebp)
 805af45:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805af4b:	8d 50 04             	lea    0x4(%eax),%edx
 805af4e:	8b 85 20 fc ff ff    	mov    -0x3e0(%ebp),%eax
 805af54:	01 d0                	add    %edx,%eax
 805af56:	3b 85 10 fc ff ff    	cmp    -0x3f0(%ebp),%eax
 805af5c:	7e 47                	jle    805afa5 <DoRtUDP()+0x5d9>
 805af5e:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805af64:	8d 50 02             	lea    0x2(%eax),%edx
 805af67:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805af6d:	83 c0 01             	add    $0x1,%eax
 805af70:	89 54 24 14          	mov    %edx,0x14(%esp)
 805af74:	89 44 24 10          	mov    %eax,0x10(%esp)
 805af78:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805af7e:	89 44 24 0c          	mov    %eax,0xc(%esp)
 805af82:	8b 85 10 fc ff ff    	mov    -0x3f0(%ebp),%eax
 805af88:	89 44 24 08          	mov    %eax,0x8(%esp)
 805af8c:	c7 44 24 04 78 a8 08 	movl   $0x808a878,0x4(%esp)
 805af93:	08 
 805af94:	c7 04 24 a0 6f 09 08 	movl   $0x8096fa0,(%esp)
 805af9b:	e8 e2 e4 ff ff       	call   8059482 <DebugLogClt::WARN_LOG(char const*, ...)>
 805afa0:	e9 b0 2a 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805afa5:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805afab:	83 c0 04             	add    $0x4,%eax
 805afae:	89 85 fc fb ff ff    	mov    %eax,-0x404(%ebp)
 805afb4:	eb 1b                	jmp    805afd1 <DoRtUDP()+0x605>
 805afb6:	8b 85 fc fb ff ff    	mov    -0x404(%ebp),%eax
 805afbc:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805afc1:	0f b6 00             	movzbl (%eax),%eax
 805afc4:	00 85 e1 fb ff ff    	add    %al,-0x41f(%ebp)
 805afca:	83 85 fc fb ff ff 01 	addl   $0x1,-0x404(%ebp)
 805afd1:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805afd7:	8d 50 04             	lea    0x4(%eax),%edx
 805afda:	8b 85 20 fc ff ff    	mov    -0x3e0(%ebp),%eax
 805afe0:	01 d0                	add    %edx,%eax
 805afe2:	3b 85 fc fb ff ff    	cmp    -0x404(%ebp),%eax
 805afe8:	7d cc                	jge    805afb6 <DoRtUDP()+0x5ea>
 805afea:	80 bd e1 fb ff ff 00 	cmpb   $0x0,-0x41f(%ebp)
 805aff1:	74 19                	je     805b00c <DoRtUDP()+0x640>
 805aff3:	c7 44 24 04 a4 a8 08 	movl   $0x808a8a4,0x4(%esp)
 805affa:	08 
 805affb:	c7 04 24 a0 6f 09 08 	movl   $0x8096fa0,(%esp)
 805b002:	e8 7b e4 ff ff       	call   8059482 <DebugLogClt::WARN_LOG(char const*, ...)>
 805b007:	e9 49 2a 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805b00c:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b012:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b017:	0f b6 00             	movzbl (%eax),%eax
 805b01a:	0f b6 c0             	movzbl %al,%eax
 805b01d:	89 85 24 fc ff ff    	mov    %eax,-0x3dc(%ebp)
 805b023:	83 bd ec fb ff ff 01 	cmpl   $0x1,-0x414(%ebp)
 805b02a:	75 07                	jne    805b033 <DoRtUDP()+0x667>
 805b02c:	83 85 f0 fb ff ff 01 	addl   $0x1,-0x410(%ebp)
 805b033:	83 bd ec fb ff ff 02 	cmpl   $0x2,-0x414(%ebp)
 805b03a:	75 07                	jne    805b043 <DoRtUDP()+0x677>
 805b03c:	83 85 f0 fb ff ff 05 	addl   $0x5,-0x410(%ebp)
 805b043:	83 bd 24 fc ff ff 65 	cmpl   $0x65,-0x3dc(%ebp)
 805b04a:	0f 85 cf 00 00 00    	jne    805b11f <DoRtUDP()+0x753>
 805b050:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b055:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805b05b:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805b061:	8b 12                	mov    (%edx),%edx
 805b063:	89 50 04             	mov    %edx,0x4(%eax)
 805b066:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b06c:	83 c0 04             	add    $0x4,%eax
 805b06f:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b075:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b07a:	c7 80 c8 00 00 00 00 	movl   $0x0,0xc8(%eax)
 805b081:	00 00 00 
 805b084:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b08a:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b08f:	89 82 cc 00 00 00    	mov    %eax,0xcc(%edx)
 805b095:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b09b:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b0a0:	89 82 d0 00 00 00    	mov    %eax,0xd0(%edx)
 805b0a6:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b0ac:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b0b1:	89 82 d4 00 00 00    	mov    %eax,0xd4(%edx)
 805b0b7:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b0bd:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b0c2:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b0c8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b0ce:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b0d3:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b0d9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b0df:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b0e4:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b0ea:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b0ef:	c6 80 74 01 00 00 01 	movb   $0x1,0x174(%eax)
 805b0f6:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b0fb:	c6 80 64 01 00 00 01 	movb   $0x1,0x164(%eax)
 805b102:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b107:	8b 40 04             	mov    0x4(%eax),%eax
 805b10a:	89 44 24 04          	mov    %eax,0x4(%esp)
 805b10e:	c7 04 24 c0 a8 08 08 	movl   $0x808a8c0,(%esp)
 805b115:	e8 56 d9 ff ff       	call   8058a70 <printf@plt>
 805b11a:	e9 1c 29 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805b11f:	83 bd 24 fc ff ff 66 	cmpl   $0x66,-0x3dc(%ebp)
 805b126:	0f 85 6c 01 00 00    	jne    805b298 <DoRtUDP()+0x8cc>
 805b12c:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b132:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b137:	89 85 28 fc ff ff    	mov    %eax,-0x3d8(%ebp)
 805b13d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b143:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b149:	8b 00                	mov    (%eax),%eax
 805b14b:	89 42 30             	mov    %eax,0x30(%edx)
 805b14e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b154:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b15a:	8b 40 04             	mov    0x4(%eax),%eax
 805b15d:	89 42 34             	mov    %eax,0x34(%edx)
 805b160:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b166:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b16c:	8b 40 08             	mov    0x8(%eax),%eax
 805b16f:	89 42 38             	mov    %eax,0x38(%edx)
 805b172:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b178:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b17e:	8b 40 0c             	mov    0xc(%eax),%eax
 805b181:	89 42 3c             	mov    %eax,0x3c(%edx)
 805b184:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b18a:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b190:	8b 40 10             	mov    0x10(%eax),%eax
 805b193:	89 42 40             	mov    %eax,0x40(%edx)
 805b196:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b19c:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b1a2:	8b 40 14             	mov    0x14(%eax),%eax
 805b1a5:	89 42 44             	mov    %eax,0x44(%edx)
 805b1a8:	c7 04 24 df a8 08 08 	movl   $0x808a8df,(%esp)
 805b1af:	e8 6c da ff ff       	call   8058c20 <puts@plt>
 805b1b4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b1ba:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b1c0:	8b 40 18             	mov    0x18(%eax),%eax
 805b1c3:	89 42 48             	mov    %eax,0x48(%edx)
 805b1c6:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b1cc:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b1d2:	8b 40 1c             	mov    0x1c(%eax),%eax
 805b1d5:	89 42 4c             	mov    %eax,0x4c(%edx)
 805b1d8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b1de:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b1e4:	8b 40 20             	mov    0x20(%eax),%eax
 805b1e7:	89 42 50             	mov    %eax,0x50(%edx)
 805b1ea:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b1f0:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b1f6:	8b 40 24             	mov    0x24(%eax),%eax
 805b1f9:	89 42 54             	mov    %eax,0x54(%edx)
 805b1fc:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b202:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b208:	8b 40 28             	mov    0x28(%eax),%eax
 805b20b:	89 42 58             	mov    %eax,0x58(%edx)
 805b20e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b214:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b21a:	8b 40 2c             	mov    0x2c(%eax),%eax
 805b21d:	89 42 5c             	mov    %eax,0x5c(%edx)
 805b220:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b226:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b22c:	8b 40 30             	mov    0x30(%eax),%eax
 805b22f:	89 42 60             	mov    %eax,0x60(%edx)
 805b232:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b238:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b23e:	8b 40 34             	mov    0x34(%eax),%eax
 805b241:	89 42 64             	mov    %eax,0x64(%edx)
 805b244:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b24a:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b250:	8b 40 38             	mov    0x38(%eax),%eax
 805b253:	89 42 68             	mov    %eax,0x68(%edx)
 805b256:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b25c:	8b 85 28 fc ff ff    	mov    -0x3d8(%ebp),%eax
 805b262:	8b 40 3c             	mov    0x3c(%eax),%eax
 805b265:	89 42 6c             	mov    %eax,0x6c(%edx)
 805b268:	ba 40 00 00 00       	mov    $0x40,%edx
 805b26d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b273:	01 d0                	add    %edx,%eax
 805b275:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b27b:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b280:	c6 80 75 01 00 00 01 	movb   $0x1,0x175(%eax)
 805b287:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b28c:	c6 80 65 01 00 00 01 	movb   $0x1,0x165(%eax)
 805b293:	e9 a3 27 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805b298:	83 bd 24 fc ff ff 67 	cmpl   $0x67,-0x3dc(%ebp)
 805b29f:	0f 85 99 00 00 00    	jne    805b33e <DoRtUDP()+0x972>
 805b2a5:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b2ab:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b2b0:	89 85 2c fc ff ff    	mov    %eax,-0x3d4(%ebp)
 805b2b6:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b2bb:	8b 95 2c fc ff ff    	mov    -0x3d4(%ebp),%edx
 805b2c1:	0f b7 12             	movzwl (%edx),%edx
 805b2c4:	66 89 90 8c 00 00 00 	mov    %dx,0x8c(%eax)
 805b2cb:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b2d0:	8b 95 2c fc ff ff    	mov    -0x3d4(%ebp),%edx
 805b2d6:	0f b7 52 02          	movzwl 0x2(%edx),%edx
 805b2da:	66 89 90 8e 00 00 00 	mov    %dx,0x8e(%eax)
 805b2e1:	8b 85 2c fc ff ff    	mov    -0x3d4(%ebp),%eax
 805b2e7:	83 c0 02             	add    $0x2,%eax
 805b2ea:	0f b7 00             	movzwl (%eax),%eax
 805b2ed:	0f bf d0             	movswl %ax,%edx
 805b2f0:	8b 85 2c fc ff ff    	mov    -0x3d4(%ebp),%eax
 805b2f6:	0f b7 00             	movzwl (%eax),%eax
 805b2f9:	98                   	cwtl   
 805b2fa:	89 54 24 08          	mov    %edx,0x8(%esp)
 805b2fe:	89 44 24 04          	mov    %eax,0x4(%esp)
 805b302:	c7 04 24 f8 a8 08 08 	movl   $0x808a8f8,(%esp)
 805b309:	e8 62 d7 ff ff       	call   8058a70 <printf@plt>
 805b30e:	ba 04 00 00 00       	mov    $0x4,%edx
 805b313:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b319:	01 d0                	add    %edx,%eax
 805b31b:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b321:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b326:	c6 80 76 01 00 00 01 	movb   $0x1,0x176(%eax)
 805b32d:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b332:	c6 80 66 01 00 00 01 	movb   $0x1,0x166(%eax)
 805b339:	e9 fd 26 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805b33e:	83 bd 24 fc ff ff 68 	cmpl   $0x68,-0x3dc(%ebp)
 805b345:	0f 85 72 01 00 00    	jne    805b4bd <DoRtUDP()+0xaf1>
 805b34b:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b351:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b356:	89 85 30 fc ff ff    	mov    %eax,-0x3d0(%ebp)
 805b35c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b362:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b368:	8b 00                	mov    (%eax),%eax
 805b36a:	89 82 90 00 00 00    	mov    %eax,0x90(%edx)
 805b370:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b376:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b37c:	8b 40 04             	mov    0x4(%eax),%eax
 805b37f:	89 82 94 00 00 00    	mov    %eax,0x94(%edx)
 805b385:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b38b:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b391:	8b 40 08             	mov    0x8(%eax),%eax
 805b394:	89 82 98 00 00 00    	mov    %eax,0x98(%edx)
 805b39a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b3a0:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b3a6:	8b 40 0c             	mov    0xc(%eax),%eax
 805b3a9:	89 82 9c 00 00 00    	mov    %eax,0x9c(%edx)
 805b3af:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b3b5:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b3bb:	8b 40 10             	mov    0x10(%eax),%eax
 805b3be:	89 82 a0 00 00 00    	mov    %eax,0xa0(%edx)
 805b3c4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b3ca:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b3d0:	8b 40 14             	mov    0x14(%eax),%eax
 805b3d3:	89 82 a4 00 00 00    	mov    %eax,0xa4(%edx)
 805b3d9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b3df:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b3e5:	8b 40 18             	mov    0x18(%eax),%eax
 805b3e8:	89 82 a8 00 00 00    	mov    %eax,0xa8(%edx)
 805b3ee:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b3f4:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b3fa:	8b 40 1c             	mov    0x1c(%eax),%eax
 805b3fd:	89 82 ac 00 00 00    	mov    %eax,0xac(%edx)
 805b403:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b409:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b40f:	8b 40 20             	mov    0x20(%eax),%eax
 805b412:	89 82 b0 00 00 00    	mov    %eax,0xb0(%edx)
 805b418:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b41e:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b424:	8b 40 24             	mov    0x24(%eax),%eax
 805b427:	89 82 b4 00 00 00    	mov    %eax,0xb4(%edx)
 805b42d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b433:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b439:	8b 40 28             	mov    0x28(%eax),%eax
 805b43c:	89 82 b8 00 00 00    	mov    %eax,0xb8(%edx)
 805b442:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b448:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b44e:	8b 40 2c             	mov    0x2c(%eax),%eax
 805b451:	89 82 bc 00 00 00    	mov    %eax,0xbc(%edx)
 805b457:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b45d:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b463:	8b 40 30             	mov    0x30(%eax),%eax
 805b466:	89 82 c0 00 00 00    	mov    %eax,0xc0(%edx)
 805b46c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b472:	8b 85 30 fc ff ff    	mov    -0x3d0(%ebp),%eax
 805b478:	8b 40 34             	mov    0x34(%eax),%eax
 805b47b:	89 82 c4 00 00 00    	mov    %eax,0xc4(%edx)
 805b481:	c7 04 24 23 a9 08 08 	movl   $0x808a923,(%esp)
 805b488:	e8 93 d7 ff ff       	call   8058c20 <puts@plt>
 805b48d:	ba 38 00 00 00       	mov    $0x38,%edx
 805b492:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b498:	01 d0                	add    %edx,%eax
 805b49a:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b4a0:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b4a5:	c6 80 77 01 00 00 01 	movb   $0x1,0x177(%eax)
 805b4ac:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b4b1:	c6 80 67 01 00 00 01 	movb   $0x1,0x167(%eax)
 805b4b8:	e9 7e 25 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805b4bd:	83 bd 24 fc ff ff 69 	cmpl   $0x69,-0x3dc(%ebp)
 805b4c4:	0f 85 a5 01 00 00    	jne    805b66f <DoRtUDP()+0xca3>
 805b4ca:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b4d0:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b4d5:	89 85 34 fc ff ff    	mov    %eax,-0x3cc(%ebp)
 805b4db:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b4e1:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b4e7:	8b 00                	mov    (%eax),%eax
 805b4e9:	89 82 e8 00 00 00    	mov    %eax,0xe8(%edx)
 805b4ef:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b4f5:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b4fb:	8b 40 04             	mov    0x4(%eax),%eax
 805b4fe:	89 82 ec 00 00 00    	mov    %eax,0xec(%edx)
 805b504:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b50a:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b510:	8b 40 08             	mov    0x8(%eax),%eax
 805b513:	89 82 f0 00 00 00    	mov    %eax,0xf0(%edx)
 805b519:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b51f:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b525:	8b 40 0c             	mov    0xc(%eax),%eax
 805b528:	89 82 f4 00 00 00    	mov    %eax,0xf4(%edx)
 805b52e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b534:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b53a:	8b 40 10             	mov    0x10(%eax),%eax
 805b53d:	89 82 f8 00 00 00    	mov    %eax,0xf8(%edx)
 805b543:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b549:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b54f:	8b 40 14             	mov    0x14(%eax),%eax
 805b552:	89 82 fc 00 00 00    	mov    %eax,0xfc(%edx)
 805b558:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b55e:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b564:	8b 40 18             	mov    0x18(%eax),%eax
 805b567:	89 82 18 01 00 00    	mov    %eax,0x118(%edx)
 805b56d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b573:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b579:	8b 40 1c             	mov    0x1c(%eax),%eax
 805b57c:	89 82 00 01 00 00    	mov    %eax,0x100(%edx)
 805b582:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b588:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b58e:	8b 40 20             	mov    0x20(%eax),%eax
 805b591:	89 82 04 01 00 00    	mov    %eax,0x104(%edx)
 805b597:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b59d:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b5a3:	8b 40 24             	mov    0x24(%eax),%eax
 805b5a6:	89 82 08 01 00 00    	mov    %eax,0x108(%edx)
 805b5ac:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b5b2:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b5b8:	8b 40 28             	mov    0x28(%eax),%eax
 805b5bb:	89 82 0c 01 00 00    	mov    %eax,0x10c(%edx)
 805b5c1:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b5c7:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b5cd:	8b 40 2c             	mov    0x2c(%eax),%eax
 805b5d0:	89 82 10 01 00 00    	mov    %eax,0x110(%edx)
 805b5d6:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b5dc:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b5e2:	8b 40 30             	mov    0x30(%eax),%eax
 805b5e5:	89 82 14 01 00 00    	mov    %eax,0x114(%edx)
 805b5eb:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b5f1:	8b 85 34 fc ff ff    	mov    -0x3cc(%ebp),%eax
 805b5f7:	8b 40 34             	mov    0x34(%eax),%eax
 805b5fa:	89 82 1c 01 00 00    	mov    %eax,0x11c(%edx)
 805b600:	c7 04 24 3e a9 08 08 	movl   $0x808a93e,(%esp)
 805b607:	e8 14 d6 ff ff       	call   8058c20 <puts@plt>
 805b60c:	ba 38 00 00 00       	mov    $0x38,%edx
 805b611:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b617:	01 d0                	add    %edx,%eax
 805b619:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b61f:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b625:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b62a:	89 85 38 fc ff ff    	mov    %eax,-0x3c8(%ebp)
 805b630:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b635:	8b 95 38 fc ff ff    	mov    -0x3c8(%ebp),%edx
 805b63b:	8b 12                	mov    (%edx),%edx
 805b63d:	89 90 e4 00 00 00    	mov    %edx,0xe4(%eax)
 805b643:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b649:	83 c0 04             	add    $0x4,%eax
 805b64c:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b652:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b657:	c6 80 78 01 00 00 01 	movb   $0x1,0x178(%eax)
 805b65e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b663:	c6 80 68 01 00 00 01 	movb   $0x1,0x168(%eax)
 805b66a:	e9 cc 23 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805b66f:	83 bd 24 fc ff ff 6a 	cmpl   $0x6a,-0x3dc(%ebp)
 805b676:	0f 85 02 03 00 00    	jne    805b97e <DoRtUDP()+0xfb2>
 805b67c:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b682:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b687:	8b 00                	mov    (%eax),%eax
 805b689:	89 85 3c fc ff ff    	mov    %eax,-0x3c4(%ebp)
 805b68f:	83 bd 3c fc ff ff 00 	cmpl   $0x0,-0x3c4(%ebp)
 805b696:	75 7a                	jne    805b712 <DoRtUDP()+0xd46>
 805b698:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b69d:	c7 80 c8 00 00 00 00 	movl   $0x0,0xc8(%eax)
 805b6a4:	00 00 00 
 805b6a7:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b6ad:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b6b2:	89 82 cc 00 00 00    	mov    %eax,0xcc(%edx)
 805b6b8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b6be:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b6c3:	89 82 d0 00 00 00    	mov    %eax,0xd0(%edx)
 805b6c9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b6cf:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b6d4:	89 82 d4 00 00 00    	mov    %eax,0xd4(%edx)
 805b6da:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b6e0:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b6e5:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b6eb:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b6f1:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b6f6:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b6fc:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b702:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b707:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b70d:	e9 21 02 00 00       	jmp    805b933 <DoRtUDP()+0xf67>
 805b712:	83 bd 3c fc ff ff 01 	cmpl   $0x1,-0x3c4(%ebp)
 805b719:	75 7a                	jne    805b795 <DoRtUDP()+0xdc9>
 805b71b:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b720:	c7 80 c8 00 00 00 01 	movl   $0x1,0xc8(%eax)
 805b727:	00 00 00 
 805b72a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b730:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b735:	89 82 cc 00 00 00    	mov    %eax,0xcc(%edx)
 805b73b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b741:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b746:	89 82 d0 00 00 00    	mov    %eax,0xd0(%edx)
 805b74c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b752:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b757:	89 82 d4 00 00 00    	mov    %eax,0xd4(%edx)
 805b75d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b763:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b768:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b76e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b774:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b779:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b77f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b785:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b78a:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b790:	e9 9e 01 00 00       	jmp    805b933 <DoRtUDP()+0xf67>
 805b795:	83 bd 3c fc ff ff 02 	cmpl   $0x2,-0x3c4(%ebp)
 805b79c:	75 58                	jne    805b7f6 <DoRtUDP()+0xe2a>
 805b79e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b7a3:	c7 80 c8 00 00 00 02 	movl   $0x2,0xc8(%eax)
 805b7aa:	00 00 00 
 805b7ad:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b7b3:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805b7b8:	89 82 cc 00 00 00    	mov    %eax,0xcc(%edx)
 805b7be:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b7c4:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b7c9:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b7cf:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b7d5:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b7da:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b7e0:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b7e6:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b7eb:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b7f1:	e9 3d 01 00 00       	jmp    805b933 <DoRtUDP()+0xf67>
 805b7f6:	83 bd 3c fc ff ff 03 	cmpl   $0x3,-0x3c4(%ebp)
 805b7fd:	75 58                	jne    805b857 <DoRtUDP()+0xe8b>
 805b7ff:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b804:	c7 80 c8 00 00 00 02 	movl   $0x2,0xc8(%eax)
 805b80b:	00 00 00 
 805b80e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b814:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805b819:	89 82 d0 00 00 00    	mov    %eax,0xd0(%edx)
 805b81f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b825:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b82a:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b830:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b836:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b83b:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b841:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b847:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b84c:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b852:	e9 dc 00 00 00       	jmp    805b933 <DoRtUDP()+0xf67>
 805b857:	83 bd 3c fc ff ff 04 	cmpl   $0x4,-0x3c4(%ebp)
 805b85e:	75 55                	jne    805b8b5 <DoRtUDP()+0xee9>
 805b860:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b865:	c7 80 c8 00 00 00 02 	movl   $0x2,0xc8(%eax)
 805b86c:	00 00 00 
 805b86f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b875:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805b87a:	89 82 d4 00 00 00    	mov    %eax,0xd4(%edx)
 805b880:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b886:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b88b:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b891:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b897:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b89c:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b8a2:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b8a8:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b8ad:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b8b3:	eb 7e                	jmp    805b933 <DoRtUDP()+0xf67>
 805b8b5:	83 bd 3c fc ff ff 05 	cmpl   $0x5,-0x3c4(%ebp)
 805b8bc:	75 75                	jne    805b933 <DoRtUDP()+0xf67>
 805b8be:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b8c3:	c7 80 c8 00 00 00 02 	movl   $0x2,0xc8(%eax)
 805b8ca:	00 00 00 
 805b8cd:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b8d3:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b8d8:	89 82 cc 00 00 00    	mov    %eax,0xcc(%edx)
 805b8de:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b8e4:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b8e9:	89 82 d0 00 00 00    	mov    %eax,0xd0(%edx)
 805b8ef:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b8f5:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b8fa:	89 82 d4 00 00 00    	mov    %eax,0xd4(%edx)
 805b900:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b906:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805b90b:	89 82 d8 00 00 00    	mov    %eax,0xd8(%edx)
 805b911:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b917:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b91c:	89 82 dc 00 00 00    	mov    %eax,0xdc(%edx)
 805b922:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b928:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805b92d:	89 82 e0 00 00 00    	mov    %eax,0xe0(%edx)
 805b933:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b939:	83 c0 04             	add    $0x4,%eax
 805b93c:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b942:	ba 18 00 00 00       	mov    $0x18,%edx
 805b947:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b94d:	01 d0                	add    %edx,%eax
 805b94f:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b955:	c7 04 24 58 a9 08 08 	movl   $0x808a958,(%esp)
 805b95c:	e8 bf d2 ff ff       	call   8058c20 <puts@plt>
 805b961:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b966:	c6 80 79 01 00 00 01 	movb   $0x1,0x179(%eax)
 805b96d:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b972:	c6 80 69 01 00 00 01 	movb   $0x1,0x169(%eax)
 805b979:	e9 bd 20 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805b97e:	83 bd 24 fc ff ff 6b 	cmpl   $0x6b,-0x3dc(%ebp)
 805b985:	0f 85 85 03 00 00    	jne    805bd10 <DoRtUDP()+0x1344>
 805b98b:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b990:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805b996:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805b99c:	8b 12                	mov    (%edx),%edx
 805b99e:	89 90 24 01 00 00    	mov    %edx,0x124(%eax)
 805b9a4:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b9aa:	83 c0 04             	add    $0x4,%eax
 805b9ad:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805b9b3:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805b9b9:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805b9be:	89 85 40 fc ff ff    	mov    %eax,-0x3c0(%ebp)
 805b9c4:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805b9c9:	8b 80 24 01 00 00    	mov    0x124(%eax),%eax
 805b9cf:	85 c0                	test   %eax,%eax
 805b9d1:	0f 85 97 00 00 00    	jne    805ba6e <DoRtUDP()+0x10a2>
 805b9d7:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b9dd:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805b9e3:	8b 00                	mov    (%eax),%eax
 805b9e5:	89 82 28 01 00 00    	mov    %eax,0x128(%edx)
 805b9eb:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805b9f1:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805b9f7:	8b 40 04             	mov    0x4(%eax),%eax
 805b9fa:	89 82 2c 01 00 00    	mov    %eax,0x12c(%edx)
 805ba00:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba06:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805ba0c:	8b 40 08             	mov    0x8(%eax),%eax
 805ba0f:	89 82 30 01 00 00    	mov    %eax,0x130(%edx)
 805ba15:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba1b:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805ba21:	8b 40 0c             	mov    0xc(%eax),%eax
 805ba24:	89 82 34 01 00 00    	mov    %eax,0x134(%edx)
 805ba2a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba30:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805ba36:	8b 40 10             	mov    0x10(%eax),%eax
 805ba39:	89 82 38 01 00 00    	mov    %eax,0x138(%edx)
 805ba3f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba45:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805ba4b:	8b 40 14             	mov    0x14(%eax),%eax
 805ba4e:	89 82 3c 01 00 00    	mov    %eax,0x13c(%edx)
 805ba54:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba5a:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805ba60:	8b 40 34             	mov    0x34(%eax),%eax
 805ba63:	89 82 5c 01 00 00    	mov    %eax,0x15c(%edx)
 805ba69:	e9 66 02 00 00       	jmp    805bcd4 <DoRtUDP()+0x1308>
 805ba6e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805ba73:	8b 80 24 01 00 00    	mov    0x124(%eax),%eax
 805ba79:	83 f8 01             	cmp    $0x1,%eax
 805ba7c:	0f 85 83 00 00 00    	jne    805bb05 <DoRtUDP()+0x1139>
 805ba82:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba88:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805ba8e:	8b 40 18             	mov    0x18(%eax),%eax
 805ba91:	89 82 40 01 00 00    	mov    %eax,0x140(%edx)
 805ba97:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ba9d:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805baa3:	8b 40 1c             	mov    0x1c(%eax),%eax
 805baa6:	89 82 44 01 00 00    	mov    %eax,0x144(%edx)
 805baac:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bab2:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bab8:	8b 40 20             	mov    0x20(%eax),%eax
 805babb:	89 82 48 01 00 00    	mov    %eax,0x148(%edx)
 805bac1:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bac7:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bacd:	8b 40 24             	mov    0x24(%eax),%eax
 805bad0:	89 82 4c 01 00 00    	mov    %eax,0x14c(%edx)
 805bad6:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805badc:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bae2:	8b 40 28             	mov    0x28(%eax),%eax
 805bae5:	89 82 50 01 00 00    	mov    %eax,0x150(%edx)
 805baeb:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805baf1:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805baf7:	8b 40 2c             	mov    0x2c(%eax),%eax
 805bafa:	89 82 54 01 00 00    	mov    %eax,0x154(%edx)
 805bb00:	e9 cf 01 00 00       	jmp    805bcd4 <DoRtUDP()+0x1308>
 805bb05:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bb0a:	8b 80 24 01 00 00    	mov    0x124(%eax),%eax
 805bb10:	83 f8 03             	cmp    $0x3,%eax
 805bb13:	0f 85 97 00 00 00    	jne    805bbb0 <DoRtUDP()+0x11e4>
 805bb19:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb1f:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bb25:	8b 00                	mov    (%eax),%eax
 805bb27:	89 82 28 01 00 00    	mov    %eax,0x128(%edx)
 805bb2d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb33:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bb39:	8b 40 04             	mov    0x4(%eax),%eax
 805bb3c:	89 82 2c 01 00 00    	mov    %eax,0x12c(%edx)
 805bb42:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb48:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bb4e:	8b 40 08             	mov    0x8(%eax),%eax
 805bb51:	89 82 30 01 00 00    	mov    %eax,0x130(%edx)
 805bb57:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb5d:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bb63:	8b 40 0c             	mov    0xc(%eax),%eax
 805bb66:	89 82 34 01 00 00    	mov    %eax,0x134(%edx)
 805bb6c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb72:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bb78:	8b 40 10             	mov    0x10(%eax),%eax
 805bb7b:	89 82 38 01 00 00    	mov    %eax,0x138(%edx)
 805bb81:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb87:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bb8d:	8b 40 14             	mov    0x14(%eax),%eax
 805bb90:	89 82 3c 01 00 00    	mov    %eax,0x13c(%edx)
 805bb96:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bb9c:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bba2:	8b 40 34             	mov    0x34(%eax),%eax
 805bba5:	89 82 5c 01 00 00    	mov    %eax,0x15c(%edx)
 805bbab:	e9 24 01 00 00       	jmp    805bcd4 <DoRtUDP()+0x1308>
 805bbb0:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bbb5:	8b 80 24 01 00 00    	mov    0x124(%eax),%eax
 805bbbb:	83 f8 04             	cmp    $0x4,%eax
 805bbbe:	0f 85 10 01 00 00    	jne    805bcd4 <DoRtUDP()+0x1308>
 805bbc4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bbca:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bbd0:	8b 00                	mov    (%eax),%eax
 805bbd2:	89 82 28 01 00 00    	mov    %eax,0x128(%edx)
 805bbd8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bbde:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bbe4:	8b 40 04             	mov    0x4(%eax),%eax
 805bbe7:	89 82 2c 01 00 00    	mov    %eax,0x12c(%edx)
 805bbed:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bbf3:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bbf9:	8b 40 08             	mov    0x8(%eax),%eax
 805bbfc:	89 82 30 01 00 00    	mov    %eax,0x130(%edx)
 805bc02:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc08:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc0e:	8b 40 0c             	mov    0xc(%eax),%eax
 805bc11:	89 82 34 01 00 00    	mov    %eax,0x134(%edx)
 805bc17:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc1d:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc23:	8b 40 10             	mov    0x10(%eax),%eax
 805bc26:	89 82 38 01 00 00    	mov    %eax,0x138(%edx)
 805bc2c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc32:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc38:	8b 40 14             	mov    0x14(%eax),%eax
 805bc3b:	89 82 3c 01 00 00    	mov    %eax,0x13c(%edx)
 805bc41:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc47:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc4d:	8b 40 18             	mov    0x18(%eax),%eax
 805bc50:	89 82 40 01 00 00    	mov    %eax,0x140(%edx)
 805bc56:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc5c:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc62:	8b 40 1c             	mov    0x1c(%eax),%eax
 805bc65:	89 82 44 01 00 00    	mov    %eax,0x144(%edx)
 805bc6b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc71:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc77:	8b 40 20             	mov    0x20(%eax),%eax
 805bc7a:	89 82 48 01 00 00    	mov    %eax,0x148(%edx)
 805bc80:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc86:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bc8c:	8b 40 24             	mov    0x24(%eax),%eax
 805bc8f:	89 82 4c 01 00 00    	mov    %eax,0x14c(%edx)
 805bc95:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bc9b:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bca1:	8b 40 28             	mov    0x28(%eax),%eax
 805bca4:	89 82 50 01 00 00    	mov    %eax,0x150(%edx)
 805bcaa:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bcb0:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bcb6:	8b 40 2c             	mov    0x2c(%eax),%eax
 805bcb9:	89 82 54 01 00 00    	mov    %eax,0x154(%edx)
 805bcbf:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bcc5:	8b 85 40 fc ff ff    	mov    -0x3c0(%ebp),%eax
 805bccb:	8b 40 34             	mov    0x34(%eax),%eax
 805bcce:	89 82 5c 01 00 00    	mov    %eax,0x15c(%edx)
 805bcd4:	c7 04 24 75 a9 08 08 	movl   $0x808a975,(%esp)
 805bcdb:	e8 40 cf ff ff       	call   8058c20 <puts@plt>
 805bce0:	ba 38 00 00 00       	mov    $0x38,%edx
 805bce5:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bceb:	01 d0                	add    %edx,%eax
 805bced:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805bcf3:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bcf8:	c6 80 7a 01 00 00 01 	movb   $0x1,0x17a(%eax)
 805bcff:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bd04:	c6 80 6a 01 00 00 01 	movb   $0x1,0x16a(%eax)
 805bd0b:	e9 2b 1d 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805bd10:	83 bd 24 fc ff ff 6c 	cmpl   $0x6c,-0x3dc(%ebp)
 805bd17:	0f 85 c6 01 00 00    	jne    805bee3 <DoRtUDP()+0x1517>
 805bd1d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bd23:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805bd28:	89 85 44 fc ff ff    	mov    %eax,-0x3bc(%ebp)
 805bd2e:	8b 85 44 fc ff ff    	mov    -0x3bc(%ebp),%eax
 805bd34:	8b 00                	mov    (%eax),%eax
 805bd36:	89 85 48 fc ff ff    	mov    %eax,-0x3b8(%ebp)
 805bd3c:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bd42:	83 c0 04             	add    $0x4,%eax
 805bd45:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805bd4b:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bd51:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805bd56:	89 85 4c fc ff ff    	mov    %eax,-0x3b4(%ebp)
 805bd5c:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bd62:	d9 00                	flds   (%eax)
 805bd64:	dd 9d c0 fc ff ff    	fstpl  -0x340(%ebp)
 805bd6a:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bd70:	83 c0 04             	add    $0x4,%eax
 805bd73:	d9 00                	flds   (%eax)
 805bd75:	dd 9d c8 fc ff ff    	fstpl  -0x338(%ebp)
 805bd7b:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bd81:	83 c0 08             	add    $0x8,%eax
 805bd84:	d9 00                	flds   (%eax)
 805bd86:	dd 9d d0 fc ff ff    	fstpl  -0x330(%ebp)
 805bd8c:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bd92:	83 c0 0c             	add    $0xc,%eax
 805bd95:	d9 00                	flds   (%eax)
 805bd97:	dd 9d d8 fc ff ff    	fstpl  -0x328(%ebp)
 805bd9d:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bda3:	83 c0 10             	add    $0x10,%eax
 805bda6:	d9 00                	flds   (%eax)
 805bda8:	dd 9d e0 fc ff ff    	fstpl  -0x320(%ebp)
 805bdae:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bdb4:	83 c0 14             	add    $0x14,%eax
 805bdb7:	d9 00                	flds   (%eax)
 805bdb9:	dd 9d e8 fc ff ff    	fstpl  -0x318(%ebp)
 805bdbf:	8b 85 4c fc ff ff    	mov    -0x3b4(%ebp),%eax
 805bdc5:	83 c0 18             	add    $0x18,%eax
 805bdc8:	d9 00                	flds   (%eax)
 805bdca:	dd 9d f0 fc ff ff    	fstpl  -0x310(%ebp)
 805bdd0:	8d 85 c0 fc ff ff    	lea    -0x340(%ebp),%eax
 805bdd6:	89 44 24 04          	mov    %eax,0x4(%esp)
 805bdda:	c7 04 24 00 00 00 00 	movl   $0x0,(%esp)
 805bde1:	e8 03 e6 ff ff       	call   805a3e9 <OnCheckJointLimt(int, double*)>
 805bde6:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bdeb:	dd 85 c0 fc ff ff    	fldl   -0x340(%ebp)
 805bdf1:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805bdf7:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805bdfd:	d9 58 70             	fstps  0x70(%eax)
 805be00:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805be05:	dd 85 c8 fc ff ff    	fldl   -0x338(%ebp)
 805be0b:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805be11:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805be17:	d9 58 74             	fstps  0x74(%eax)
 805be1a:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805be1f:	dd 85 d0 fc ff ff    	fldl   -0x330(%ebp)
 805be25:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805be2b:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805be31:	d9 58 78             	fstps  0x78(%eax)
 805be34:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805be39:	dd 85 d8 fc ff ff    	fldl   -0x328(%ebp)
 805be3f:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805be45:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805be4b:	d9 58 7c             	fstps  0x7c(%eax)
 805be4e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805be53:	dd 85 e0 fc ff ff    	fldl   -0x320(%ebp)
 805be59:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805be5f:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805be65:	d9 98 80 00 00 00    	fstps  0x80(%eax)
 805be6b:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805be70:	dd 85 e8 fc ff ff    	fldl   -0x318(%ebp)
 805be76:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805be7c:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805be82:	d9 98 84 00 00 00    	fstps  0x84(%eax)
 805be88:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805be8d:	dd 85 f0 fc ff ff    	fldl   -0x310(%ebp)
 805be93:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805be99:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805be9f:	d9 98 88 00 00 00    	fstps  0x88(%eax)
 805bea5:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805beaa:	8b 95 48 fc ff ff    	mov    -0x3b8(%ebp),%edx
 805beb0:	89 50 20             	mov    %edx,0x20(%eax)
 805beb3:	ba 1c 00 00 00       	mov    $0x1c,%edx
 805beb8:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bebe:	01 d0                	add    %edx,%eax
 805bec0:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805bec6:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805becb:	c6 80 7b 01 00 00 01 	movb   $0x1,0x17b(%eax)
 805bed2:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bed7:	c6 80 6b 01 00 00 01 	movb   $0x1,0x16b(%eax)
 805bede:	e9 58 1b 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805bee3:	83 bd 24 fc ff ff 6d 	cmpl   $0x6d,-0x3dc(%ebp)
 805beea:	75 5d                	jne    805bf49 <DoRtUDP()+0x157d>
 805beec:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bef2:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805bef7:	89 85 50 fc ff ff    	mov    %eax,-0x3b0(%ebp)
 805befd:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805bf03:	8b 85 50 fc ff ff    	mov    -0x3b0(%ebp),%eax
 805bf09:	8b 00                	mov    (%eax),%eax
 805bf0b:	89 82 60 01 00 00    	mov    %eax,0x160(%edx)
 805bf11:	c7 04 24 93 a9 08 08 	movl   $0x808a993,(%esp)
 805bf18:	e8 03 cd ff ff       	call   8058c20 <puts@plt>
 805bf1d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bf23:	83 c0 04             	add    $0x4,%eax
 805bf26:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805bf2c:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bf31:	c6 80 7c 01 00 00 01 	movb   $0x1,0x17c(%eax)
 805bf38:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bf3d:	c6 80 6c 01 00 00 01 	movb   $0x1,0x16c(%eax)
 805bf44:	e9 f2 1a 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805bf49:	83 bd 24 fc ff ff 6e 	cmpl   $0x6e,-0x3dc(%ebp)
 805bf50:	75 51                	jne    805bfa3 <DoRtUDP()+0x15d7>
 805bf52:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bf58:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805bf5d:	89 85 54 fc ff ff    	mov    %eax,-0x3ac(%ebp)
 805bf63:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bf68:	8b 95 54 fc ff ff    	mov    -0x3ac(%ebp),%edx
 805bf6e:	0f b6 12             	movzbl (%edx),%edx
 805bf71:	88 90 84 01 00 00    	mov    %dl,0x184(%eax)
 805bf77:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bf7c:	c6 80 85 01 00 00 01 	movb   $0x1,0x185(%eax)
 805bf83:	c7 04 24 af a9 08 08 	movl   $0x808a9af,(%esp)
 805bf8a:	e8 91 cc ff ff       	call   8058c20 <puts@plt>
 805bf8f:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bf95:	83 c0 01             	add    $0x1,%eax
 805bf98:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805bf9e:	e9 98 1a 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805bfa3:	83 bd 24 fc ff ff 6f 	cmpl   $0x6f,-0x3dc(%ebp)
 805bfaa:	75 5a                	jne    805c006 <DoRtUDP()+0x163a>
 805bfac:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bfb1:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805bfb7:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805bfbd:	8b 12                	mov    (%edx),%edx
 805bfbf:	89 50 1c             	mov    %edx,0x1c(%eax)
 805bfc2:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805bfc8:	83 c0 04             	add    $0x4,%eax
 805bfcb:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805bfd1:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bfd6:	c6 80 7d 01 00 00 01 	movb   $0x1,0x17d(%eax)
 805bfdd:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bfe2:	c6 80 6d 01 00 00 01 	movb   $0x1,0x16d(%eax)
 805bfe9:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805bfee:	8b 40 1c             	mov    0x1c(%eax),%eax
 805bff1:	89 44 24 04          	mov    %eax,0x4(%esp)
 805bff5:	c7 04 24 cc a9 08 08 	movl   $0x808a9cc,(%esp)
 805bffc:	e8 6f ca ff ff       	call   8058a70 <printf@plt>
 805c001:	e9 35 1a 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c006:	83 bd 24 fc ff ff 70 	cmpl   $0x70,-0x3dc(%ebp)
 805c00d:	0f 85 ed 00 00 00    	jne    805c100 <DoRtUDP()+0x1734>
 805c013:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c019:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c01e:	8b 00                	mov    (%eax),%eax
 805c020:	89 85 58 fc ff ff    	mov    %eax,-0x3a8(%ebp)
 805c026:	83 bd 58 fc ff ff 04 	cmpl   $0x4,-0x3a8(%ebp)
 805c02d:	0f 8e c8 00 00 00    	jle    805c0fb <DoRtUDP()+0x172f>
 805c033:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c038:	c7 44 24 08 54 23 02 	movl   $0x22354,0x8(%esp)
 805c03f:	00 
 805c040:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805c047:	00 
 805c048:	89 04 24             	mov    %eax,(%esp)
 805c04b:	e8 50 c7 ff ff       	call   80587a0 <memset@plt>
 805c050:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c055:	8b 95 58 fc ff ff    	mov    -0x3a8(%ebp),%edx
 805c05b:	89 10                	mov    %edx,(%eax)
 805c05d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c063:	83 c0 04             	add    $0x4,%eax
 805c066:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c06c:	8b 1d a8 68 09 08    	mov    0x80968a8,%ebx
 805c072:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c077:	8b 08                	mov    (%eax),%ecx
 805c079:	ba 1f 85 eb 51       	mov    $0x51eb851f,%edx
 805c07e:	89 c8                	mov    %ecx,%eax
 805c080:	f7 ea                	imul   %edx
 805c082:	c1 fa 04             	sar    $0x4,%edx
 805c085:	89 c8                	mov    %ecx,%eax
 805c087:	c1 f8 1f             	sar    $0x1f,%eax
 805c08a:	29 c2                	sub    %eax,%edx
 805c08c:	89 d0                	mov    %edx,%eax
 805c08e:	89 43 04             	mov    %eax,0x4(%ebx)
 805c091:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c096:	8b 08                	mov    (%eax),%ecx
 805c098:	ba 1f 85 eb 51       	mov    $0x51eb851f,%edx
 805c09d:	89 c8                	mov    %ecx,%eax
 805c09f:	f7 ea                	imul   %edx
 805c0a1:	c1 fa 04             	sar    $0x4,%edx
 805c0a4:	89 c8                	mov    %ecx,%eax
 805c0a6:	c1 f8 1f             	sar    $0x1f,%eax
 805c0a9:	29 c2                	sub    %eax,%edx
 805c0ab:	89 d0                	mov    %edx,%eax
 805c0ad:	6b c0 32             	imul   $0x32,%eax,%eax
 805c0b0:	29 c1                	sub    %eax,%ecx
 805c0b2:	89 c8                	mov    %ecx,%eax
 805c0b4:	85 c0                	test   %eax,%eax
 805c0b6:	74 14                	je     805c0cc <DoRtUDP()+0x1700>
 805c0b8:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c0bd:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c0c3:	8b 52 04             	mov    0x4(%edx),%edx
 805c0c6:	83 c2 01             	add    $0x1,%edx
 805c0c9:	89 50 04             	mov    %edx,0x4(%eax)
 805c0cc:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c0d1:	c6 80 2f 04 00 00 01 	movb   $0x1,0x42f(%eax)
 805c0d8:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c0dd:	8b 50 04             	mov    0x4(%eax),%edx
 805c0e0:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c0e5:	8b 00                	mov    (%eax),%eax
 805c0e7:	89 54 24 08          	mov    %edx,0x8(%esp)
 805c0eb:	89 44 24 04          	mov    %eax,0x4(%esp)
 805c0ef:	c7 04 24 ec a9 08 08 	movl   $0x808a9ec,(%esp)
 805c0f6:	e8 75 c9 ff ff       	call   8058a70 <printf@plt>
 805c0fb:	e9 3b 19 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c100:	83 bd 24 fc ff ff 79 	cmpl   $0x79,-0x3dc(%ebp)
 805c107:	75 48                	jne    805c151 <DoRtUDP()+0x1785>
 805c109:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c10f:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c115:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c11a:	8b 00                	mov    (%eax),%eax
 805c11c:	89 82 58 01 00 00    	mov    %eax,0x158(%edx)
 805c122:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c128:	83 c0 04             	add    $0x4,%eax
 805c12b:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c131:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c136:	d9 80 58 01 00 00    	flds   0x158(%eax)
 805c13c:	dd 5c 24 04          	fstpl  0x4(%esp)
 805c140:	c7 04 24 30 aa 08 08 	movl   $0x808aa30,(%esp)
 805c147:	e8 24 c9 ff ff       	call   8058a70 <printf@plt>
 805c14c:	e9 ea 18 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c151:	83 bd 24 fc ff ff 71 	cmpl   $0x71,-0x3dc(%ebp)
 805c158:	0f 85 67 02 00 00    	jne    805c3c5 <DoRtUDP()+0x19f9>
 805c15e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c163:	0f b6 80 2f 04 00 00 	movzbl 0x42f(%eax),%eax
 805c16a:	3c 01                	cmp    $0x1,%al
 805c16c:	0f 85 4e 02 00 00    	jne    805c3c0 <DoRtUDP()+0x19f4>
 805c172:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c178:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c17d:	8b 00                	mov    (%eax),%eax
 805c17f:	89 85 5c fc ff ff    	mov    %eax,-0x3a4(%ebp)
 805c185:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c18b:	83 c0 04             	add    $0x4,%eax
 805c18e:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c194:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c199:	8b 40 04             	mov    0x4(%eax),%eax
 805c19c:	83 e8 01             	sub    $0x1,%eax
 805c19f:	3b 85 5c fc ff ff    	cmp    -0x3a4(%ebp),%eax
 805c1a5:	0f 8e b2 00 00 00    	jle    805c25d <DoRtUDP()+0x1891>
 805c1ab:	b9 78 05 00 00       	mov    $0x578,%ecx
 805c1b0:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c1b6:	8d 98 e0 62 09 08    	lea    0x80962e0(%eax),%ebx
 805c1bc:	8b 35 a8 68 09 08    	mov    0x80968a8,%esi
 805c1c2:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c1c8:	6b c0 32             	imul   $0x32,%eax,%eax
 805c1cb:	c1 e0 02             	shl    $0x2,%eax
 805c1ce:	8d 14 c5 00 00 00 00 	lea    0x0(,%eax,8),%edx
 805c1d5:	29 c2                	sub    %eax,%edx
 805c1d7:	8d 42 70             	lea    0x70(%edx),%eax
 805c1da:	01 f0                	add    %esi,%eax
 805c1dc:	83 c0 04             	add    $0x4,%eax
 805c1df:	89 4c 24 08          	mov    %ecx,0x8(%esp)
 805c1e3:	89 5c 24 04          	mov    %ebx,0x4(%esp)
 805c1e7:	89 04 24             	mov    %eax,(%esp)
 805c1ea:	e8 c1 c7 ff ff       	call   80589b0 <memcpy@plt>
 805c1ef:	ba 78 05 00 00       	mov    $0x578,%edx
 805c1f4:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c1fa:	01 d0                	add    %edx,%eax
 805c1fc:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c202:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c208:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c20e:	01 d0                	add    %edx,%eax
 805c210:	83 c0 10             	add    $0x10,%eax
 805c213:	0f b6 00             	movzbl (%eax),%eax
 805c216:	84 c0                	test   %al,%al
 805c218:	75 28                	jne    805c242 <DoRtUDP()+0x1876>
 805c21a:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c220:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c226:	01 d0                	add    %edx,%eax
 805c228:	83 c0 10             	add    $0x10,%eax
 805c22b:	c6 00 01             	movb   $0x1,(%eax)
 805c22e:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c233:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c239:	8b 52 08             	mov    0x8(%edx),%edx
 805c23c:	83 c2 32             	add    $0x32,%edx
 805c23f:	89 50 08             	mov    %edx,0x8(%eax)
 805c242:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c248:	89 44 24 04          	mov    %eax,0x4(%esp)
 805c24c:	c7 04 24 54 aa 08 08 	movl   $0x808aa54,(%esp)
 805c253:	e8 18 c8 ff ff       	call   8058a70 <printf@plt>
 805c258:	e9 33 01 00 00       	jmp    805c390 <DoRtUDP()+0x19c4>
 805c25d:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c262:	8b 40 04             	mov    0x4(%eax),%eax
 805c265:	83 e8 01             	sub    $0x1,%eax
 805c268:	3b 85 5c fc ff ff    	cmp    -0x3a4(%ebp),%eax
 805c26e:	0f 85 1c 01 00 00    	jne    805c390 <DoRtUDP()+0x19c4>
 805c274:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c279:	8b 08                	mov    (%eax),%ecx
 805c27b:	ba 1f 85 eb 51       	mov    $0x51eb851f,%edx
 805c280:	89 c8                	mov    %ecx,%eax
 805c282:	f7 ea                	imul   %edx
 805c284:	c1 fa 04             	sar    $0x4,%edx
 805c287:	89 c8                	mov    %ecx,%eax
 805c289:	c1 f8 1f             	sar    $0x1f,%eax
 805c28c:	29 c2                	sub    %eax,%edx
 805c28e:	89 d0                	mov    %edx,%eax
 805c290:	89 85 00 fc ff ff    	mov    %eax,-0x400(%ebp)
 805c296:	8b 85 00 fc ff ff    	mov    -0x400(%ebp),%eax
 805c29c:	6b c0 32             	imul   $0x32,%eax,%eax
 805c29f:	29 c1                	sub    %eax,%ecx
 805c2a1:	89 c8                	mov    %ecx,%eax
 805c2a3:	89 85 00 fc ff ff    	mov    %eax,-0x400(%ebp)
 805c2a9:	83 bd 00 fc ff ff 00 	cmpl   $0x0,-0x400(%ebp)
 805c2b0:	75 0a                	jne    805c2bc <DoRtUDP()+0x18f0>
 805c2b2:	c7 85 00 fc ff ff 32 	movl   $0x32,-0x400(%ebp)
 805c2b9:	00 00 00 
 805c2bc:	8b 85 00 fc ff ff    	mov    -0x400(%ebp),%eax
 805c2c2:	8d 14 85 00 00 00 00 	lea    0x0(,%eax,4),%edx
 805c2c9:	89 d0                	mov    %edx,%eax
 805c2cb:	c1 e0 03             	shl    $0x3,%eax
 805c2ce:	29 d0                	sub    %edx,%eax
 805c2d0:	89 c3                	mov    %eax,%ebx
 805c2d2:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c2d8:	8d 88 e0 62 09 08    	lea    0x80962e0(%eax),%ecx
 805c2de:	8b 35 a8 68 09 08    	mov    0x80968a8,%esi
 805c2e4:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c2ea:	6b c0 32             	imul   $0x32,%eax,%eax
 805c2ed:	c1 e0 02             	shl    $0x2,%eax
 805c2f0:	8d 14 c5 00 00 00 00 	lea    0x0(,%eax,8),%edx
 805c2f7:	29 c2                	sub    %eax,%edx
 805c2f9:	8d 42 70             	lea    0x70(%edx),%eax
 805c2fc:	01 f0                	add    %esi,%eax
 805c2fe:	83 c0 04             	add    $0x4,%eax
 805c301:	89 5c 24 08          	mov    %ebx,0x8(%esp)
 805c305:	89 4c 24 04          	mov    %ecx,0x4(%esp)
 805c309:	89 04 24             	mov    %eax,(%esp)
 805c30c:	e8 9f c6 ff ff       	call   80589b0 <memcpy@plt>
 805c311:	8b 85 00 fc ff ff    	mov    -0x400(%ebp),%eax
 805c317:	8d 14 85 00 00 00 00 	lea    0x0(,%eax,4),%edx
 805c31e:	89 d0                	mov    %edx,%eax
 805c320:	c1 e0 03             	shl    $0x3,%eax
 805c323:	29 d0                	sub    %edx,%eax
 805c325:	89 c2                	mov    %eax,%edx
 805c327:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c32d:	01 d0                	add    %edx,%eax
 805c32f:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c335:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c33b:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c341:	01 d0                	add    %edx,%eax
 805c343:	83 c0 10             	add    $0x10,%eax
 805c346:	0f b6 00             	movzbl (%eax),%eax
 805c349:	84 c0                	test   %al,%al
 805c34b:	75 2d                	jne    805c37a <DoRtUDP()+0x19ae>
 805c34d:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c353:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c359:	01 d0                	add    %edx,%eax
 805c35b:	83 c0 10             	add    $0x10,%eax
 805c35e:	c6 00 01             	movb   $0x1,(%eax)
 805c361:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c366:	8b 15 a8 68 09 08    	mov    0x80968a8,%edx
 805c36c:	8b 4a 08             	mov    0x8(%edx),%ecx
 805c36f:	8b 95 00 fc ff ff    	mov    -0x400(%ebp),%edx
 805c375:	01 ca                	add    %ecx,%edx
 805c377:	89 50 08             	mov    %edx,0x8(%eax)
 805c37a:	8b 85 5c fc ff ff    	mov    -0x3a4(%ebp),%eax
 805c380:	89 44 24 04          	mov    %eax,0x4(%esp)
 805c384:	c7 04 24 8c aa 08 08 	movl   $0x808aa8c,(%esp)
 805c38b:	e8 e0 c6 ff ff       	call   8058a70 <printf@plt>
 805c390:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c395:	8b 50 08             	mov    0x8(%eax),%edx
 805c398:	a1 a8 68 09 08       	mov    0x80968a8,%eax
 805c39d:	8b 00                	mov    (%eax),%eax
 805c39f:	39 c2                	cmp    %eax,%edx
 805c3a1:	75 18                	jne    805c3bb <DoRtUDP()+0x19ef>
 805c3a3:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c3a8:	c6 80 2f 04 00 00 02 	movb   $0x2,0x42f(%eax)
 805c3af:	c7 04 24 cc aa 08 08 	movl   $0x808aacc,(%esp)
 805c3b6:	e8 65 c8 ff ff       	call   8058c20 <puts@plt>
 805c3bb:	e9 7b 16 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c3c0:	e9 90 16 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805c3c5:	83 bd 24 fc ff ff 72 	cmpl   $0x72,-0x3dc(%ebp)
 805c3cc:	75 32                	jne    805c400 <DoRtUDP()+0x1a34>
 805c3ce:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c3d3:	0f b6 80 2f 04 00 00 	movzbl 0x42f(%eax),%eax
 805c3da:	3c 02                	cmp    $0x2,%al
 805c3dc:	75 1d                	jne    805c3fb <DoRtUDP()+0x1a2f>
 805c3de:	c7 04 24 fc aa 08 08 	movl   $0x808aafc,(%esp)
 805c3e5:	e8 36 c8 ff ff       	call   8058c20 <puts@plt>
 805c3ea:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c3ef:	c6 80 2f 04 00 00 03 	movb   $0x3,0x42f(%eax)
 805c3f6:	e9 40 16 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c3fb:	e9 55 16 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805c400:	83 bd 24 fc ff ff 73 	cmpl   $0x73,-0x3dc(%ebp)
 805c407:	75 32                	jne    805c43b <DoRtUDP()+0x1a6f>
 805c409:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c40e:	0f b6 80 2f 04 00 00 	movzbl 0x42f(%eax),%eax
 805c415:	3c 02                	cmp    $0x2,%al
 805c417:	7e 1d                	jle    805c436 <DoRtUDP()+0x1a6a>
 805c419:	c7 04 24 30 ab 08 08 	movl   $0x808ab30,(%esp)
 805c420:	e8 fb c7 ff ff       	call   8058c20 <puts@plt>
 805c425:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c42a:	c6 80 2f 04 00 00 09 	movb   $0x9,0x42f(%eax)
 805c431:	e9 05 16 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c436:	e9 1a 16 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805c43b:	81 bd 24 fc ff ff c9 	cmpl   $0xc9,-0x3dc(%ebp)
 805c442:	00 00 00 
 805c445:	0f 85 cf 00 00 00    	jne    805c51a <DoRtUDP()+0x1b4e>
 805c44b:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c450:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805c456:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805c45c:	8b 12                	mov    (%edx),%edx
 805c45e:	89 50 10             	mov    %edx,0x10(%eax)
 805c461:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c467:	83 c0 04             	add    $0x4,%eax
 805c46a:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c470:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c475:	c7 80 38 02 00 00 00 	movl   $0x0,0x238(%eax)
 805c47c:	00 00 00 
 805c47f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c485:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805c48a:	89 82 3c 02 00 00    	mov    %eax,0x23c(%edx)
 805c490:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c496:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805c49b:	89 82 40 02 00 00    	mov    %eax,0x240(%edx)
 805c4a1:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c4a7:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805c4ac:	89 82 44 02 00 00    	mov    %eax,0x244(%edx)
 805c4b2:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c4b8:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805c4bd:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805c4c3:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c4c9:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805c4ce:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805c4d4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c4da:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805c4df:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805c4e5:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c4ea:	c6 80 e4 02 00 00 01 	movb   $0x1,0x2e4(%eax)
 805c4f1:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c4f6:	c6 80 d4 02 00 00 01 	movb   $0x1,0x2d4(%eax)
 805c4fd:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c502:	8b 40 10             	mov    0x10(%eax),%eax
 805c505:	89 44 24 04          	mov    %eax,0x4(%esp)
 805c509:	c7 04 24 64 ab 08 08 	movl   $0x808ab64,(%esp)
 805c510:	e8 5b c5 ff ff       	call   8058a70 <printf@plt>
 805c515:	e9 21 15 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c51a:	81 bd 24 fc ff ff ca 	cmpl   $0xca,-0x3dc(%ebp)
 805c521:	00 00 00 
 805c524:	0f 85 9c 01 00 00    	jne    805c6c6 <DoRtUDP()+0x1cfa>
 805c52a:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c530:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c535:	89 85 60 fc ff ff    	mov    %eax,-0x3a0(%ebp)
 805c53b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c541:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c547:	8b 00                	mov    (%eax),%eax
 805c549:	89 82 a0 01 00 00    	mov    %eax,0x1a0(%edx)
 805c54f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c555:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c55b:	8b 40 04             	mov    0x4(%eax),%eax
 805c55e:	89 82 a4 01 00 00    	mov    %eax,0x1a4(%edx)
 805c564:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c56a:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c570:	8b 40 08             	mov    0x8(%eax),%eax
 805c573:	89 82 a8 01 00 00    	mov    %eax,0x1a8(%edx)
 805c579:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c57f:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c585:	8b 40 0c             	mov    0xc(%eax),%eax
 805c588:	89 82 ac 01 00 00    	mov    %eax,0x1ac(%edx)
 805c58e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c594:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c59a:	8b 40 10             	mov    0x10(%eax),%eax
 805c59d:	89 82 b0 01 00 00    	mov    %eax,0x1b0(%edx)
 805c5a3:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c5a9:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c5af:	8b 40 14             	mov    0x14(%eax),%eax
 805c5b2:	89 82 b4 01 00 00    	mov    %eax,0x1b4(%edx)
 805c5b8:	c7 04 24 83 ab 08 08 	movl   $0x808ab83,(%esp)
 805c5bf:	e8 5c c6 ff ff       	call   8058c20 <puts@plt>
 805c5c4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c5ca:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c5d0:	8b 40 18             	mov    0x18(%eax),%eax
 805c5d3:	89 82 b8 01 00 00    	mov    %eax,0x1b8(%edx)
 805c5d9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c5df:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c5e5:	8b 40 1c             	mov    0x1c(%eax),%eax
 805c5e8:	89 82 bc 01 00 00    	mov    %eax,0x1bc(%edx)
 805c5ee:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c5f4:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c5fa:	8b 40 20             	mov    0x20(%eax),%eax
 805c5fd:	89 82 c0 01 00 00    	mov    %eax,0x1c0(%edx)
 805c603:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c609:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c60f:	8b 40 24             	mov    0x24(%eax),%eax
 805c612:	89 82 c4 01 00 00    	mov    %eax,0x1c4(%edx)
 805c618:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c61e:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c624:	8b 40 28             	mov    0x28(%eax),%eax
 805c627:	89 82 c8 01 00 00    	mov    %eax,0x1c8(%edx)
 805c62d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c633:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c639:	8b 40 2c             	mov    0x2c(%eax),%eax
 805c63c:	89 82 cc 01 00 00    	mov    %eax,0x1cc(%edx)
 805c642:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c648:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c64e:	8b 40 30             	mov    0x30(%eax),%eax
 805c651:	89 82 d0 01 00 00    	mov    %eax,0x1d0(%edx)
 805c657:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c65d:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c663:	8b 40 34             	mov    0x34(%eax),%eax
 805c666:	89 82 d4 01 00 00    	mov    %eax,0x1d4(%edx)
 805c66c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c672:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c678:	8b 40 38             	mov    0x38(%eax),%eax
 805c67b:	89 82 d8 01 00 00    	mov    %eax,0x1d8(%edx)
 805c681:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c687:	8b 85 60 fc ff ff    	mov    -0x3a0(%ebp),%eax
 805c68d:	8b 40 3c             	mov    0x3c(%eax),%eax
 805c690:	89 82 dc 01 00 00    	mov    %eax,0x1dc(%edx)
 805c696:	ba 40 00 00 00       	mov    $0x40,%edx
 805c69b:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c6a1:	01 d0                	add    %edx,%eax
 805c6a3:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c6a9:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c6ae:	c6 80 e5 02 00 00 01 	movb   $0x1,0x2e5(%eax)
 805c6b5:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c6ba:	c6 80 d5 02 00 00 01 	movb   $0x1,0x2d5(%eax)
 805c6c1:	e9 75 13 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c6c6:	81 bd 24 fc ff ff cb 	cmpl   $0xcb,-0x3dc(%ebp)
 805c6cd:	00 00 00 
 805c6d0:	0f 85 99 00 00 00    	jne    805c76f <DoRtUDP()+0x1da3>
 805c6d6:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c6dc:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c6e1:	89 85 64 fc ff ff    	mov    %eax,-0x39c(%ebp)
 805c6e7:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c6ec:	8b 95 64 fc ff ff    	mov    -0x39c(%ebp),%edx
 805c6f2:	0f b7 12             	movzwl (%edx),%edx
 805c6f5:	66 89 90 fc 01 00 00 	mov    %dx,0x1fc(%eax)
 805c6fc:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c701:	8b 95 64 fc ff ff    	mov    -0x39c(%ebp),%edx
 805c707:	0f b7 52 02          	movzwl 0x2(%edx),%edx
 805c70b:	66 89 90 fe 01 00 00 	mov    %dx,0x1fe(%eax)
 805c712:	8b 85 64 fc ff ff    	mov    -0x39c(%ebp),%eax
 805c718:	83 c0 02             	add    $0x2,%eax
 805c71b:	0f b7 00             	movzwl (%eax),%eax
 805c71e:	0f bf d0             	movswl %ax,%edx
 805c721:	8b 85 64 fc ff ff    	mov    -0x39c(%ebp),%eax
 805c727:	0f b7 00             	movzwl (%eax),%eax
 805c72a:	98                   	cwtl   
 805c72b:	89 54 24 08          	mov    %edx,0x8(%esp)
 805c72f:	89 44 24 04          	mov    %eax,0x4(%esp)
 805c733:	c7 04 24 9c ab 08 08 	movl   $0x808ab9c,(%esp)
 805c73a:	e8 31 c3 ff ff       	call   8058a70 <printf@plt>
 805c73f:	ba 04 00 00 00       	mov    $0x4,%edx
 805c744:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c74a:	01 d0                	add    %edx,%eax
 805c74c:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c752:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c757:	c6 80 e6 02 00 00 01 	movb   $0x1,0x2e6(%eax)
 805c75e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c763:	c6 80 d6 02 00 00 01 	movb   $0x1,0x2d6(%eax)
 805c76a:	e9 cc 12 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c76f:	81 bd 24 fc ff ff cc 	cmpl   $0xcc,-0x3dc(%ebp)
 805c776:	00 00 00 
 805c779:	0f 85 72 01 00 00    	jne    805c8f1 <DoRtUDP()+0x1f25>
 805c77f:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c785:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c78a:	89 85 68 fc ff ff    	mov    %eax,-0x398(%ebp)
 805c790:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c796:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c79c:	8b 00                	mov    (%eax),%eax
 805c79e:	89 82 00 02 00 00    	mov    %eax,0x200(%edx)
 805c7a4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c7aa:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c7b0:	8b 40 04             	mov    0x4(%eax),%eax
 805c7b3:	89 82 04 02 00 00    	mov    %eax,0x204(%edx)
 805c7b9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c7bf:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c7c5:	8b 40 08             	mov    0x8(%eax),%eax
 805c7c8:	89 82 08 02 00 00    	mov    %eax,0x208(%edx)
 805c7ce:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c7d4:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c7da:	8b 40 0c             	mov    0xc(%eax),%eax
 805c7dd:	89 82 0c 02 00 00    	mov    %eax,0x20c(%edx)
 805c7e3:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c7e9:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c7ef:	8b 40 10             	mov    0x10(%eax),%eax
 805c7f2:	89 82 10 02 00 00    	mov    %eax,0x210(%edx)
 805c7f8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c7fe:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c804:	8b 40 14             	mov    0x14(%eax),%eax
 805c807:	89 82 14 02 00 00    	mov    %eax,0x214(%edx)
 805c80d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c813:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c819:	8b 40 18             	mov    0x18(%eax),%eax
 805c81c:	89 82 18 02 00 00    	mov    %eax,0x218(%edx)
 805c822:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c828:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c82e:	8b 40 1c             	mov    0x1c(%eax),%eax
 805c831:	89 82 1c 02 00 00    	mov    %eax,0x21c(%edx)
 805c837:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c83d:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c843:	8b 40 20             	mov    0x20(%eax),%eax
 805c846:	89 82 20 02 00 00    	mov    %eax,0x220(%edx)
 805c84c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c852:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c858:	8b 40 24             	mov    0x24(%eax),%eax
 805c85b:	89 82 24 02 00 00    	mov    %eax,0x224(%edx)
 805c861:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c867:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c86d:	8b 40 28             	mov    0x28(%eax),%eax
 805c870:	89 82 28 02 00 00    	mov    %eax,0x228(%edx)
 805c876:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c87c:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c882:	8b 40 2c             	mov    0x2c(%eax),%eax
 805c885:	89 82 2c 02 00 00    	mov    %eax,0x22c(%edx)
 805c88b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c891:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c897:	8b 40 30             	mov    0x30(%eax),%eax
 805c89a:	89 82 30 02 00 00    	mov    %eax,0x230(%edx)
 805c8a0:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c8a6:	8b 85 68 fc ff ff    	mov    -0x398(%ebp),%eax
 805c8ac:	8b 40 34             	mov    0x34(%eax),%eax
 805c8af:	89 82 34 02 00 00    	mov    %eax,0x234(%edx)
 805c8b5:	c7 04 24 c7 ab 08 08 	movl   $0x808abc7,(%esp)
 805c8bc:	e8 5f c3 ff ff       	call   8058c20 <puts@plt>
 805c8c1:	ba 38 00 00 00       	mov    $0x38,%edx
 805c8c6:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c8cc:	01 d0                	add    %edx,%eax
 805c8ce:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805c8d4:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c8d9:	c6 80 e7 02 00 00 01 	movb   $0x1,0x2e7(%eax)
 805c8e0:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805c8e5:	c6 80 d7 02 00 00 01 	movb   $0x1,0x2d7(%eax)
 805c8ec:	e9 4a 11 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805c8f1:	81 bd 24 fc ff ff cd 	cmpl   $0xcd,-0x3dc(%ebp)
 805c8f8:	00 00 00 
 805c8fb:	0f 85 a5 01 00 00    	jne    805caa6 <DoRtUDP()+0x20da>
 805c901:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805c907:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805c90c:	89 85 6c fc ff ff    	mov    %eax,-0x394(%ebp)
 805c912:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c918:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c91e:	8b 00                	mov    (%eax),%eax
 805c920:	89 82 58 02 00 00    	mov    %eax,0x258(%edx)
 805c926:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c92c:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c932:	8b 40 04             	mov    0x4(%eax),%eax
 805c935:	89 82 5c 02 00 00    	mov    %eax,0x25c(%edx)
 805c93b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c941:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c947:	8b 40 08             	mov    0x8(%eax),%eax
 805c94a:	89 82 60 02 00 00    	mov    %eax,0x260(%edx)
 805c950:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c956:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c95c:	8b 40 0c             	mov    0xc(%eax),%eax
 805c95f:	89 82 64 02 00 00    	mov    %eax,0x264(%edx)
 805c965:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c96b:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c971:	8b 40 10             	mov    0x10(%eax),%eax
 805c974:	89 82 68 02 00 00    	mov    %eax,0x268(%edx)
 805c97a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c980:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c986:	8b 40 14             	mov    0x14(%eax),%eax
 805c989:	89 82 6c 02 00 00    	mov    %eax,0x26c(%edx)
 805c98f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c995:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c99b:	8b 40 18             	mov    0x18(%eax),%eax
 805c99e:	89 82 88 02 00 00    	mov    %eax,0x288(%edx)
 805c9a4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c9aa:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c9b0:	8b 40 1c             	mov    0x1c(%eax),%eax
 805c9b3:	89 82 70 02 00 00    	mov    %eax,0x270(%edx)
 805c9b9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c9bf:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c9c5:	8b 40 20             	mov    0x20(%eax),%eax
 805c9c8:	89 82 74 02 00 00    	mov    %eax,0x274(%edx)
 805c9ce:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c9d4:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c9da:	8b 40 24             	mov    0x24(%eax),%eax
 805c9dd:	89 82 78 02 00 00    	mov    %eax,0x278(%edx)
 805c9e3:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c9e9:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805c9ef:	8b 40 28             	mov    0x28(%eax),%eax
 805c9f2:	89 82 7c 02 00 00    	mov    %eax,0x27c(%edx)
 805c9f8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805c9fe:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805ca04:	8b 40 2c             	mov    0x2c(%eax),%eax
 805ca07:	89 82 80 02 00 00    	mov    %eax,0x280(%edx)
 805ca0d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ca13:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805ca19:	8b 40 30             	mov    0x30(%eax),%eax
 805ca1c:	89 82 84 02 00 00    	mov    %eax,0x284(%edx)
 805ca22:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ca28:	8b 85 6c fc ff ff    	mov    -0x394(%ebp),%eax
 805ca2e:	8b 40 34             	mov    0x34(%eax),%eax
 805ca31:	89 82 8c 02 00 00    	mov    %eax,0x28c(%edx)
 805ca37:	c7 04 24 e2 ab 08 08 	movl   $0x808abe2,(%esp)
 805ca3e:	e8 dd c1 ff ff       	call   8058c20 <puts@plt>
 805ca43:	ba 38 00 00 00       	mov    $0x38,%edx
 805ca48:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ca4e:	01 d0                	add    %edx,%eax
 805ca50:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805ca56:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ca5c:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805ca61:	89 85 70 fc ff ff    	mov    %eax,-0x390(%ebp)
 805ca67:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805ca6c:	8b 95 70 fc ff ff    	mov    -0x390(%ebp),%edx
 805ca72:	8b 12                	mov    (%edx),%edx
 805ca74:	89 90 54 02 00 00    	mov    %edx,0x254(%eax)
 805ca7a:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ca80:	83 c0 04             	add    $0x4,%eax
 805ca83:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805ca89:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805ca8e:	c6 80 e8 02 00 00 01 	movb   $0x1,0x2e8(%eax)
 805ca95:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805ca9a:	c6 80 d8 02 00 00 01 	movb   $0x1,0x2d8(%eax)
 805caa1:	e9 95 0f 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805caa6:	81 bd 24 fc ff ff ce 	cmpl   $0xce,-0x3dc(%ebp)
 805caad:	00 00 00 
 805cab0:	0f 85 16 03 00 00    	jne    805cdcc <DoRtUDP()+0x2400>
 805cab6:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805cabc:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805cac1:	8b 00                	mov    (%eax),%eax
 805cac3:	89 85 74 fc ff ff    	mov    %eax,-0x38c(%ebp)
 805cac9:	83 bd 74 fc ff ff 00 	cmpl   $0x0,-0x38c(%ebp)
 805cad0:	75 7a                	jne    805cb4c <DoRtUDP()+0x2180>
 805cad2:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cad7:	c7 80 38 02 00 00 00 	movl   $0x0,0x238(%eax)
 805cade:	00 00 00 
 805cae1:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cae7:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805caec:	89 82 3c 02 00 00    	mov    %eax,0x23c(%edx)
 805caf2:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805caf8:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cafd:	89 82 40 02 00 00    	mov    %eax,0x240(%edx)
 805cb03:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb09:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb0e:	89 82 44 02 00 00    	mov    %eax,0x244(%edx)
 805cb14:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb1a:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb1f:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805cb25:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb2b:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb30:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805cb36:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb3c:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb41:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805cb47:	e9 2b 02 00 00       	jmp    805cd77 <DoRtUDP()+0x23ab>
 805cb4c:	83 bd 74 fc ff ff 01 	cmpl   $0x1,-0x38c(%ebp)
 805cb53:	75 7a                	jne    805cbcf <DoRtUDP()+0x2203>
 805cb55:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cb5a:	c7 80 38 02 00 00 01 	movl   $0x1,0x238(%eax)
 805cb61:	00 00 00 
 805cb64:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb6a:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb6f:	89 82 3c 02 00 00    	mov    %eax,0x23c(%edx)
 805cb75:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb7b:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb80:	89 82 40 02 00 00    	mov    %eax,0x240(%edx)
 805cb86:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb8c:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cb91:	89 82 44 02 00 00    	mov    %eax,0x244(%edx)
 805cb97:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cb9d:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cba2:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805cba8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cbae:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cbb3:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805cbb9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cbbf:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cbc4:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805cbca:	e9 a8 01 00 00       	jmp    805cd77 <DoRtUDP()+0x23ab>
 805cbcf:	83 bd 74 fc ff ff 02 	cmpl   $0x2,-0x38c(%ebp)
 805cbd6:	75 58                	jne    805cc30 <DoRtUDP()+0x2264>
 805cbd8:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cbdd:	c7 80 38 02 00 00 02 	movl   $0x2,0x238(%eax)
 805cbe4:	00 00 00 
 805cbe7:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cbed:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805cbf2:	89 82 3c 02 00 00    	mov    %eax,0x23c(%edx)
 805cbf8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cbfe:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cc03:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805cc09:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cc0f:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cc14:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805cc1a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cc20:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cc25:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805cc2b:	e9 47 01 00 00       	jmp    805cd77 <DoRtUDP()+0x23ab>
 805cc30:	83 bd 74 fc ff ff 03 	cmpl   $0x3,-0x38c(%ebp)
 805cc37:	75 58                	jne    805cc91 <DoRtUDP()+0x22c5>
 805cc39:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cc3e:	c7 80 38 02 00 00 02 	movl   $0x2,0x238(%eax)
 805cc45:	00 00 00 
 805cc48:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cc4e:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805cc53:	89 82 40 02 00 00    	mov    %eax,0x240(%edx)
 805cc59:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cc5f:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cc64:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805cc6a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cc70:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cc75:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805cc7b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cc81:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cc86:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805cc8c:	e9 e6 00 00 00       	jmp    805cd77 <DoRtUDP()+0x23ab>
 805cc91:	83 bd 74 fc ff ff 04 	cmpl   $0x4,-0x38c(%ebp)
 805cc98:	75 58                	jne    805ccf2 <DoRtUDP()+0x2326>
 805cc9a:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cc9f:	c7 80 38 02 00 00 02 	movl   $0x2,0x238(%eax)
 805cca6:	00 00 00 
 805cca9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ccaf:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805ccb4:	89 82 44 02 00 00    	mov    %eax,0x244(%edx)
 805ccba:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ccc0:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805ccc5:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805cccb:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ccd1:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805ccd6:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805ccdc:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cce2:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cce7:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805cced:	e9 85 00 00 00       	jmp    805cd77 <DoRtUDP()+0x23ab>
 805ccf2:	83 bd 74 fc ff ff 05 	cmpl   $0x5,-0x38c(%ebp)
 805ccf9:	75 77                	jne    805cd72 <DoRtUDP()+0x23a6>
 805ccfb:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cd00:	c7 80 38 02 00 00 02 	movl   $0x2,0x238(%eax)
 805cd07:	00 00 00 
 805cd0a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cd10:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cd15:	89 82 3c 02 00 00    	mov    %eax,0x23c(%edx)
 805cd1b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cd21:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cd26:	89 82 40 02 00 00    	mov    %eax,0x240(%edx)
 805cd2c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cd32:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cd37:	89 82 44 02 00 00    	mov    %eax,0x244(%edx)
 805cd3d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cd43:	a1 4c ae 08 08       	mov    0x808ae4c,%eax
 805cd48:	89 82 48 02 00 00    	mov    %eax,0x248(%edx)
 805cd4e:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cd54:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cd59:	89 82 4c 02 00 00    	mov    %eax,0x24c(%edx)
 805cd5f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cd65:	a1 48 ae 08 08       	mov    0x808ae48,%eax
 805cd6a:	89 82 50 02 00 00    	mov    %eax,0x250(%edx)
 805cd70:	eb 05                	jmp    805cd77 <DoRtUDP()+0x23ab>
 805cd72:	e9 de 0c 00 00       	jmp    805da55 <DoRtUDP()+0x3089>
 805cd77:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805cd7d:	83 c0 04             	add    $0x4,%eax
 805cd80:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805cd86:	ba 18 00 00 00       	mov    $0x18,%edx
 805cd8b:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805cd91:	01 d0                	add    %edx,%eax
 805cd93:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805cd99:	8b 85 74 fc ff ff    	mov    -0x38c(%ebp),%eax
 805cd9f:	89 44 24 04          	mov    %eax,0x4(%esp)
 805cda3:	c7 04 24 fc ab 08 08 	movl   $0x808abfc,(%esp)
 805cdaa:	e8 c1 bc ff ff       	call   8058a70 <printf@plt>
 805cdaf:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cdb4:	c6 80 e9 02 00 00 01 	movb   $0x1,0x2e9(%eax)
 805cdbb:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cdc0:	c6 80 d9 02 00 00 01 	movb   $0x1,0x2d9(%eax)
 805cdc7:	e9 6f 0c 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805cdcc:	81 bd 24 fc ff ff cf 	cmpl   $0xcf,-0x3dc(%ebp)
 805cdd3:	00 00 00 
 805cdd6:	0f 85 c6 02 00 00    	jne    805d0a2 <DoRtUDP()+0x26d6>
 805cddc:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cde1:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805cde7:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805cded:	8b 12                	mov    (%edx),%edx
 805cdef:	89 90 94 02 00 00    	mov    %edx,0x294(%eax)
 805cdf5:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805cdfb:	83 c0 04             	add    $0x4,%eax
 805cdfe:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805ce04:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805ce0a:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805ce0f:	89 85 78 fc ff ff    	mov    %eax,-0x388(%ebp)
 805ce15:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805ce1a:	8b 80 94 02 00 00    	mov    0x294(%eax),%eax
 805ce20:	85 c0                	test   %eax,%eax
 805ce22:	0f 85 97 00 00 00    	jne    805cebf <DoRtUDP()+0x24f3>
 805ce28:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ce2e:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ce34:	8b 00                	mov    (%eax),%eax
 805ce36:	89 82 98 02 00 00    	mov    %eax,0x298(%edx)
 805ce3c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ce42:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ce48:	8b 40 04             	mov    0x4(%eax),%eax
 805ce4b:	89 82 9c 02 00 00    	mov    %eax,0x29c(%edx)
 805ce51:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ce57:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ce5d:	8b 40 08             	mov    0x8(%eax),%eax
 805ce60:	89 82 a0 02 00 00    	mov    %eax,0x2a0(%edx)
 805ce66:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ce6c:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ce72:	8b 40 0c             	mov    0xc(%eax),%eax
 805ce75:	89 82 a4 02 00 00    	mov    %eax,0x2a4(%edx)
 805ce7b:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ce81:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ce87:	8b 40 10             	mov    0x10(%eax),%eax
 805ce8a:	89 82 a8 02 00 00    	mov    %eax,0x2a8(%edx)
 805ce90:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ce96:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ce9c:	8b 40 14             	mov    0x14(%eax),%eax
 805ce9f:	89 82 ac 02 00 00    	mov    %eax,0x2ac(%edx)
 805cea5:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ceab:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805ceb1:	8b 40 34             	mov    0x34(%eax),%eax
 805ceb4:	89 82 cc 02 00 00    	mov    %eax,0x2cc(%edx)
 805ceba:	e9 a7 01 00 00       	jmp    805d066 <DoRtUDP()+0x269a>
 805cebf:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805cec4:	8b 80 94 02 00 00    	mov    0x294(%eax),%eax
 805ceca:	83 f8 01             	cmp    $0x1,%eax
 805cecd:	0f 85 83 00 00 00    	jne    805cf56 <DoRtUDP()+0x258a>
 805ced3:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ced9:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cedf:	8b 40 18             	mov    0x18(%eax),%eax
 805cee2:	89 82 b0 02 00 00    	mov    %eax,0x2b0(%edx)
 805cee8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805ceee:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cef4:	8b 40 1c             	mov    0x1c(%eax),%eax
 805cef7:	89 82 b4 02 00 00    	mov    %eax,0x2b4(%edx)
 805cefd:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf03:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf09:	8b 40 20             	mov    0x20(%eax),%eax
 805cf0c:	89 82 b8 02 00 00    	mov    %eax,0x2b8(%edx)
 805cf12:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf18:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf1e:	8b 40 24             	mov    0x24(%eax),%eax
 805cf21:	89 82 bc 02 00 00    	mov    %eax,0x2bc(%edx)
 805cf27:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf2d:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf33:	8b 40 28             	mov    0x28(%eax),%eax
 805cf36:	89 82 c0 02 00 00    	mov    %eax,0x2c0(%edx)
 805cf3c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf42:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf48:	8b 40 2c             	mov    0x2c(%eax),%eax
 805cf4b:	89 82 c4 02 00 00    	mov    %eax,0x2c4(%edx)
 805cf51:	e9 10 01 00 00       	jmp    805d066 <DoRtUDP()+0x269a>
 805cf56:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf5c:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf62:	8b 00                	mov    (%eax),%eax
 805cf64:	89 82 98 02 00 00    	mov    %eax,0x298(%edx)
 805cf6a:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf70:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf76:	8b 40 04             	mov    0x4(%eax),%eax
 805cf79:	89 82 9c 02 00 00    	mov    %eax,0x29c(%edx)
 805cf7f:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf85:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cf8b:	8b 40 08             	mov    0x8(%eax),%eax
 805cf8e:	89 82 a0 02 00 00    	mov    %eax,0x2a0(%edx)
 805cf94:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cf9a:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cfa0:	8b 40 0c             	mov    0xc(%eax),%eax
 805cfa3:	89 82 a4 02 00 00    	mov    %eax,0x2a4(%edx)
 805cfa9:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cfaf:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cfb5:	8b 40 10             	mov    0x10(%eax),%eax
 805cfb8:	89 82 a8 02 00 00    	mov    %eax,0x2a8(%edx)
 805cfbe:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cfc4:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cfca:	8b 40 14             	mov    0x14(%eax),%eax
 805cfcd:	89 82 ac 02 00 00    	mov    %eax,0x2ac(%edx)
 805cfd3:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cfd9:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cfdf:	8b 40 18             	mov    0x18(%eax),%eax
 805cfe2:	89 82 b0 02 00 00    	mov    %eax,0x2b0(%edx)
 805cfe8:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805cfee:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805cff4:	8b 40 1c             	mov    0x1c(%eax),%eax
 805cff7:	89 82 b4 02 00 00    	mov    %eax,0x2b4(%edx)
 805cffd:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d003:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805d009:	8b 40 20             	mov    0x20(%eax),%eax
 805d00c:	89 82 b8 02 00 00    	mov    %eax,0x2b8(%edx)
 805d012:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d018:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805d01e:	8b 40 24             	mov    0x24(%eax),%eax
 805d021:	89 82 bc 02 00 00    	mov    %eax,0x2bc(%edx)
 805d027:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d02d:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805d033:	8b 40 28             	mov    0x28(%eax),%eax
 805d036:	89 82 c0 02 00 00    	mov    %eax,0x2c0(%edx)
 805d03c:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d042:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805d048:	8b 40 2c             	mov    0x2c(%eax),%eax
 805d04b:	89 82 c4 02 00 00    	mov    %eax,0x2c4(%edx)
 805d051:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d057:	8b 85 78 fc ff ff    	mov    -0x388(%ebp),%eax
 805d05d:	8b 40 34             	mov    0x34(%eax),%eax
 805d060:	89 82 cc 02 00 00    	mov    %eax,0x2cc(%edx)
 805d066:	c7 04 24 1a ac 08 08 	movl   $0x808ac1a,(%esp)
 805d06d:	e8 ae bb ff ff       	call   8058c20 <puts@plt>
 805d072:	ba 38 00 00 00       	mov    $0x38,%edx
 805d077:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d07d:	01 d0                	add    %edx,%eax
 805d07f:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d085:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d08a:	c6 80 ea 02 00 00 01 	movb   $0x1,0x2ea(%eax)
 805d091:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d096:	c6 80 da 02 00 00 01 	movb   $0x1,0x2da(%eax)
 805d09d:	e9 99 09 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d0a2:	81 bd 24 fc ff ff d0 	cmpl   $0xd0,-0x3dc(%ebp)
 805d0a9:	00 00 00 
 805d0ac:	0f 85 d5 01 00 00    	jne    805d287 <DoRtUDP()+0x28bb>
 805d0b2:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d0b8:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d0bd:	89 85 7c fc ff ff    	mov    %eax,-0x384(%ebp)
 805d0c3:	8b 85 7c fc ff ff    	mov    -0x384(%ebp),%eax
 805d0c9:	8b 00                	mov    (%eax),%eax
 805d0cb:	89 85 80 fc ff ff    	mov    %eax,-0x380(%ebp)
 805d0d1:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d0d7:	83 c0 04             	add    $0x4,%eax
 805d0da:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d0e0:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d0e6:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d0eb:	89 85 84 fc ff ff    	mov    %eax,-0x37c(%ebp)
 805d0f1:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d0f7:	d9 00                	flds   (%eax)
 805d0f9:	dd 9d c0 fc ff ff    	fstpl  -0x340(%ebp)
 805d0ff:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d105:	83 c0 04             	add    $0x4,%eax
 805d108:	d9 00                	flds   (%eax)
 805d10a:	dd 9d c8 fc ff ff    	fstpl  -0x338(%ebp)
 805d110:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d116:	83 c0 08             	add    $0x8,%eax
 805d119:	d9 00                	flds   (%eax)
 805d11b:	dd 9d d0 fc ff ff    	fstpl  -0x330(%ebp)
 805d121:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d127:	83 c0 0c             	add    $0xc,%eax
 805d12a:	d9 00                	flds   (%eax)
 805d12c:	dd 9d d8 fc ff ff    	fstpl  -0x328(%ebp)
 805d132:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d138:	83 c0 10             	add    $0x10,%eax
 805d13b:	d9 00                	flds   (%eax)
 805d13d:	dd 9d e0 fc ff ff    	fstpl  -0x320(%ebp)
 805d143:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d149:	83 c0 14             	add    $0x14,%eax
 805d14c:	d9 00                	flds   (%eax)
 805d14e:	dd 9d e8 fc ff ff    	fstpl  -0x318(%ebp)
 805d154:	8b 85 84 fc ff ff    	mov    -0x37c(%ebp),%eax
 805d15a:	83 c0 18             	add    $0x18,%eax
 805d15d:	d9 00                	flds   (%eax)
 805d15f:	dd 9d f0 fc ff ff    	fstpl  -0x310(%ebp)
 805d165:	8d 85 c0 fc ff ff    	lea    -0x340(%ebp),%eax
 805d16b:	89 44 24 04          	mov    %eax,0x4(%esp)
 805d16f:	c7 04 24 01 00 00 00 	movl   $0x1,(%esp)
 805d176:	e8 6e d2 ff ff       	call   805a3e9 <OnCheckJointLimt(int, double*)>
 805d17b:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d180:	dd 85 c0 fc ff ff    	fldl   -0x340(%ebp)
 805d186:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d18c:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d192:	d9 98 e0 01 00 00    	fstps  0x1e0(%eax)
 805d198:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d19d:	dd 85 c8 fc ff ff    	fldl   -0x338(%ebp)
 805d1a3:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d1a9:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d1af:	d9 98 e4 01 00 00    	fstps  0x1e4(%eax)
 805d1b5:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d1ba:	dd 85 d0 fc ff ff    	fldl   -0x330(%ebp)
 805d1c0:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d1c6:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d1cc:	d9 98 e8 01 00 00    	fstps  0x1e8(%eax)
 805d1d2:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d1d7:	dd 85 d8 fc ff ff    	fldl   -0x328(%ebp)
 805d1dd:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d1e3:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d1e9:	d9 98 ec 01 00 00    	fstps  0x1ec(%eax)
 805d1ef:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d1f4:	dd 85 e0 fc ff ff    	fldl   -0x320(%ebp)
 805d1fa:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d200:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d206:	d9 98 f0 01 00 00    	fstps  0x1f0(%eax)
 805d20c:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d211:	dd 85 e8 fc ff ff    	fldl   -0x318(%ebp)
 805d217:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d21d:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d223:	d9 98 f4 01 00 00    	fstps  0x1f4(%eax)
 805d229:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d22e:	dd 85 f0 fc ff ff    	fldl   -0x310(%ebp)
 805d234:	d9 9d d4 fb ff ff    	fstps  -0x42c(%ebp)
 805d23a:	d9 85 d4 fb ff ff    	flds   -0x42c(%ebp)
 805d240:	d9 98 f8 01 00 00    	fstps  0x1f8(%eax)
 805d246:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d24b:	8b 95 80 fc ff ff    	mov    -0x380(%ebp),%edx
 805d251:	89 90 90 01 00 00    	mov    %edx,0x190(%eax)
 805d257:	ba 1c 00 00 00       	mov    $0x1c,%edx
 805d25c:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d262:	01 d0                	add    %edx,%eax
 805d264:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d26a:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d26f:	c6 80 eb 02 00 00 01 	movb   $0x1,0x2eb(%eax)
 805d276:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d27b:	c6 80 db 02 00 00 01 	movb   $0x1,0x2db(%eax)
 805d282:	e9 b4 07 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d287:	81 bd 24 fc ff ff d1 	cmpl   $0xd1,-0x3dc(%ebp)
 805d28e:	00 00 00 
 805d291:	75 5d                	jne    805d2f0 <DoRtUDP()+0x2924>
 805d293:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d299:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d29e:	89 85 88 fc ff ff    	mov    %eax,-0x378(%ebp)
 805d2a4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d2aa:	8b 85 88 fc ff ff    	mov    -0x378(%ebp),%eax
 805d2b0:	8b 00                	mov    (%eax),%eax
 805d2b2:	89 82 d0 02 00 00    	mov    %eax,0x2d0(%edx)
 805d2b8:	c7 04 24 38 ac 08 08 	movl   $0x808ac38,(%esp)
 805d2bf:	e8 5c b9 ff ff       	call   8058c20 <puts@plt>
 805d2c4:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d2ca:	83 c0 04             	add    $0x4,%eax
 805d2cd:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d2d3:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d2d8:	c6 80 ec 02 00 00 01 	movb   $0x1,0x2ec(%eax)
 805d2df:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d2e4:	c6 80 dc 02 00 00 01 	movb   $0x1,0x2dc(%eax)
 805d2eb:	e9 4b 07 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d2f0:	81 bd 24 fc ff ff d1 	cmpl   $0xd1,-0x3dc(%ebp)
 805d2f7:	00 00 00 
 805d2fa:	75 5d                	jne    805d359 <DoRtUDP()+0x298d>
 805d2fc:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d302:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d307:	89 85 8c fc ff ff    	mov    %eax,-0x374(%ebp)
 805d30d:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d313:	8b 85 8c fc ff ff    	mov    -0x374(%ebp),%eax
 805d319:	8b 00                	mov    (%eax),%eax
 805d31b:	89 82 d0 02 00 00    	mov    %eax,0x2d0(%edx)
 805d321:	c7 04 24 38 ac 08 08 	movl   $0x808ac38,(%esp)
 805d328:	e8 f3 b8 ff ff       	call   8058c20 <puts@plt>
 805d32d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d333:	83 c0 04             	add    $0x4,%eax
 805d336:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d33c:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d341:	c6 80 ec 02 00 00 01 	movb   $0x1,0x2ec(%eax)
 805d348:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d34d:	c6 80 dc 02 00 00 01 	movb   $0x1,0x2dc(%eax)
 805d354:	e9 e2 06 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d359:	81 bd 24 fc ff ff d2 	cmpl   $0xd2,-0x3dc(%ebp)
 805d360:	00 00 00 
 805d363:	75 51                	jne    805d3b6 <DoRtUDP()+0x29ea>
 805d365:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d36b:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d370:	89 85 90 fc ff ff    	mov    %eax,-0x370(%ebp)
 805d376:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d37b:	8b 95 90 fc ff ff    	mov    -0x370(%ebp),%edx
 805d381:	0f b6 12             	movzbl (%edx),%edx
 805d384:	88 90 f4 02 00 00    	mov    %dl,0x2f4(%eax)
 805d38a:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d38f:	c6 80 f5 02 00 00 01 	movb   $0x1,0x2f5(%eax)
 805d396:	c7 04 24 54 ac 08 08 	movl   $0x808ac54,(%esp)
 805d39d:	e8 7e b8 ff ff       	call   8058c20 <puts@plt>
 805d3a2:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d3a8:	83 c0 01             	add    $0x1,%eax
 805d3ab:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d3b1:	e9 85 06 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d3b6:	81 bd 24 fc ff ff d3 	cmpl   $0xd3,-0x3dc(%ebp)
 805d3bd:	00 00 00 
 805d3c0:	75 6c                	jne    805d42e <DoRtUDP()+0x2a62>
 805d3c2:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d3c7:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805d3cd:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805d3d3:	8b 12                	mov    (%edx),%edx
 805d3d5:	89 90 8c 01 00 00    	mov    %edx,0x18c(%eax)
 805d3db:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d3e1:	83 c0 04             	add    $0x4,%eax
 805d3e4:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d3ea:	c7 04 24 6e ac 08 08 	movl   $0x808ac6e,(%esp)
 805d3f1:	e8 2a b8 ff ff       	call   8058c20 <puts@plt>
 805d3f6:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d3fb:	c6 80 ed 02 00 00 01 	movb   $0x1,0x2ed(%eax)
 805d402:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d407:	c6 80 dd 02 00 00 01 	movb   $0x1,0x2dd(%eax)
 805d40e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d413:	8b 80 8c 01 00 00    	mov    0x18c(%eax),%eax
 805d419:	89 44 24 04          	mov    %eax,0x4(%esp)
 805d41d:	c7 04 24 89 ac 08 08 	movl   $0x808ac89,(%esp)
 805d424:	e8 47 b6 ff ff       	call   8058a70 <printf@plt>
 805d429:	e9 0d 06 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d42e:	81 bd 24 fc ff ff d4 	cmpl   $0xd4,-0x3dc(%ebp)
 805d435:	00 00 00 
 805d438:	0f 85 eb 00 00 00    	jne    805d529 <DoRtUDP()+0x2b5d>
 805d43e:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d444:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d449:	8b 00                	mov    (%eax),%eax
 805d44b:	83 f8 04             	cmp    $0x4,%eax
 805d44e:	0f 8e e7 05 00 00    	jle    805da3b <DoRtUDP()+0x306f>
 805d454:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d459:	c7 44 24 08 54 23 02 	movl   $0x22354,0x8(%esp)
 805d460:	00 
 805d461:	c7 44 24 04 00 00 00 	movl   $0x0,0x4(%esp)
 805d468:	00 
 805d469:	89 04 24             	mov    %eax,(%esp)
 805d46c:	e8 2f b3 ff ff       	call   80587a0 <memset@plt>
 805d471:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d476:	8b 95 f0 fb ff ff    	mov    -0x410(%ebp),%edx
 805d47c:	81 c2 e0 62 09 08    	add    $0x80962e0,%edx
 805d482:	8b 12                	mov    (%edx),%edx
 805d484:	89 10                	mov    %edx,(%eax)
 805d486:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d48c:	83 c0 04             	add    $0x4,%eax
 805d48f:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d495:	8b 1d ac 68 09 08    	mov    0x80968ac,%ebx
 805d49b:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d4a0:	8b 08                	mov    (%eax),%ecx
 805d4a2:	ba 1f 85 eb 51       	mov    $0x51eb851f,%edx
 805d4a7:	89 c8                	mov    %ecx,%eax
 805d4a9:	f7 ea                	imul   %edx
 805d4ab:	c1 fa 04             	sar    $0x4,%edx
 805d4ae:	89 c8                	mov    %ecx,%eax
 805d4b0:	c1 f8 1f             	sar    $0x1f,%eax
 805d4b3:	29 c2                	sub    %eax,%edx
 805d4b5:	89 d0                	mov    %edx,%eax
 805d4b7:	89 43 04             	mov    %eax,0x4(%ebx)
 805d4ba:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d4bf:	8b 08                	mov    (%eax),%ecx
 805d4c1:	ba 1f 85 eb 51       	mov    $0x51eb851f,%edx
 805d4c6:	89 c8                	mov    %ecx,%eax
 805d4c8:	f7 ea                	imul   %edx
 805d4ca:	c1 fa 04             	sar    $0x4,%edx
 805d4cd:	89 c8                	mov    %ecx,%eax
 805d4cf:	c1 f8 1f             	sar    $0x1f,%eax
 805d4d2:	29 c2                	sub    %eax,%edx
 805d4d4:	89 d0                	mov    %edx,%eax
 805d4d6:	6b c0 32             	imul   $0x32,%eax,%eax
 805d4d9:	29 c1                	sub    %eax,%ecx
 805d4db:	89 c8                	mov    %ecx,%eax
 805d4dd:	85 c0                	test   %eax,%eax
 805d4df:	74 14                	je     805d4f5 <DoRtUDP()+0x2b29>
 805d4e1:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d4e6:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d4ec:	8b 52 04             	mov    0x4(%edx),%edx
 805d4ef:	83 c2 01             	add    $0x1,%edx
 805d4f2:	89 50 04             	mov    %edx,0x4(%eax)
 805d4f5:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d4fa:	c6 80 67 05 00 00 01 	movb   $0x1,0x567(%eax)
 805d501:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d506:	8b 50 04             	mov    0x4(%eax),%edx
 805d509:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d50e:	8b 00                	mov    (%eax),%eax
 805d510:	89 54 24 08          	mov    %edx,0x8(%esp)
 805d514:	89 44 24 04          	mov    %eax,0x4(%esp)
 805d518:	c7 04 24 90 ac 08 08 	movl   $0x808ac90,(%esp)
 805d51f:	e8 4c b5 ff ff       	call   8058a70 <printf@plt>
 805d524:	e9 12 05 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d529:	81 bd 24 fc ff ff d5 	cmpl   $0xd5,-0x3dc(%ebp)
 805d530:	00 00 00 
 805d533:	0f 85 62 02 00 00    	jne    805d79b <DoRtUDP()+0x2dcf>
 805d539:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d53e:	0f b6 80 67 05 00 00 	movzbl 0x567(%eax),%eax
 805d545:	3c 01                	cmp    $0x1,%al
 805d547:	0f 85 49 02 00 00    	jne    805d796 <DoRtUDP()+0x2dca>
 805d54d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d553:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d558:	8b 00                	mov    (%eax),%eax
 805d55a:	89 85 94 fc ff ff    	mov    %eax,-0x36c(%ebp)
 805d560:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d566:	83 c0 04             	add    $0x4,%eax
 805d569:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d56f:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d574:	8b 40 04             	mov    0x4(%eax),%eax
 805d577:	83 e8 01             	sub    $0x1,%eax
 805d57a:	3b 85 94 fc ff ff    	cmp    -0x36c(%ebp),%eax
 805d580:	0f 8e b2 00 00 00    	jle    805d638 <DoRtUDP()+0x2c6c>
 805d586:	b9 78 05 00 00       	mov    $0x578,%ecx
 805d58b:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d591:	8d 98 e0 62 09 08    	lea    0x80962e0(%eax),%ebx
 805d597:	8b 35 ac 68 09 08    	mov    0x80968ac,%esi
 805d59d:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d5a3:	6b c0 32             	imul   $0x32,%eax,%eax
 805d5a6:	c1 e0 02             	shl    $0x2,%eax
 805d5a9:	8d 14 c5 00 00 00 00 	lea    0x0(,%eax,8),%edx
 805d5b0:	29 c2                	sub    %eax,%edx
 805d5b2:	8d 42 70             	lea    0x70(%edx),%eax
 805d5b5:	01 f0                	add    %esi,%eax
 805d5b7:	83 c0 04             	add    $0x4,%eax
 805d5ba:	89 4c 24 08          	mov    %ecx,0x8(%esp)
 805d5be:	89 5c 24 04          	mov    %ebx,0x4(%esp)
 805d5c2:	89 04 24             	mov    %eax,(%esp)
 805d5c5:	e8 e6 b3 ff ff       	call   80589b0 <memcpy@plt>
 805d5ca:	ba 78 05 00 00       	mov    $0x578,%edx
 805d5cf:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d5d5:	01 d0                	add    %edx,%eax
 805d5d7:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d5dd:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d5e3:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d5e9:	01 d0                	add    %edx,%eax
 805d5eb:	83 c0 10             	add    $0x10,%eax
 805d5ee:	0f b6 00             	movzbl (%eax),%eax
 805d5f1:	84 c0                	test   %al,%al
 805d5f3:	75 28                	jne    805d61d <DoRtUDP()+0x2c51>
 805d5f5:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d5fb:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d601:	01 d0                	add    %edx,%eax
 805d603:	83 c0 10             	add    $0x10,%eax
 805d606:	c6 00 01             	movb   $0x1,(%eax)
 805d609:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d60e:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d614:	8b 52 08             	mov    0x8(%edx),%edx
 805d617:	83 c2 32             	add    $0x32,%edx
 805d61a:	89 50 08             	mov    %edx,0x8(%eax)
 805d61d:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d623:	89 44 24 04          	mov    %eax,0x4(%esp)
 805d627:	c7 04 24 d4 ac 08 08 	movl   $0x808acd4,(%esp)
 805d62e:	e8 3d b4 ff ff       	call   8058a70 <printf@plt>
 805d633:	e9 33 01 00 00       	jmp    805d76b <DoRtUDP()+0x2d9f>
 805d638:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d63d:	8b 40 04             	mov    0x4(%eax),%eax
 805d640:	83 e8 01             	sub    $0x1,%eax
 805d643:	3b 85 94 fc ff ff    	cmp    -0x36c(%ebp),%eax
 805d649:	0f 85 1c 01 00 00    	jne    805d76b <DoRtUDP()+0x2d9f>
 805d64f:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d654:	8b 08                	mov    (%eax),%ecx
 805d656:	ba 1f 85 eb 51       	mov    $0x51eb851f,%edx
 805d65b:	89 c8                	mov    %ecx,%eax
 805d65d:	f7 ea                	imul   %edx
 805d65f:	c1 fa 04             	sar    $0x4,%edx
 805d662:	89 c8                	mov    %ecx,%eax
 805d664:	c1 f8 1f             	sar    $0x1f,%eax
 805d667:	29 c2                	sub    %eax,%edx
 805d669:	89 d0                	mov    %edx,%eax
 805d66b:	89 85 04 fc ff ff    	mov    %eax,-0x3fc(%ebp)
 805d671:	8b 85 04 fc ff ff    	mov    -0x3fc(%ebp),%eax
 805d677:	6b c0 32             	imul   $0x32,%eax,%eax
 805d67a:	29 c1                	sub    %eax,%ecx
 805d67c:	89 c8                	mov    %ecx,%eax
 805d67e:	89 85 04 fc ff ff    	mov    %eax,-0x3fc(%ebp)
 805d684:	83 bd 04 fc ff ff 00 	cmpl   $0x0,-0x3fc(%ebp)
 805d68b:	75 0a                	jne    805d697 <DoRtUDP()+0x2ccb>
 805d68d:	c7 85 04 fc ff ff 32 	movl   $0x32,-0x3fc(%ebp)
 805d694:	00 00 00 
 805d697:	8b 85 04 fc ff ff    	mov    -0x3fc(%ebp),%eax
 805d69d:	8d 14 85 00 00 00 00 	lea    0x0(,%eax,4),%edx
 805d6a4:	89 d0                	mov    %edx,%eax
 805d6a6:	c1 e0 03             	shl    $0x3,%eax
 805d6a9:	29 d0                	sub    %edx,%eax
 805d6ab:	89 c3                	mov    %eax,%ebx
 805d6ad:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d6b3:	8d 88 e0 62 09 08    	lea    0x80962e0(%eax),%ecx
 805d6b9:	8b 35 ac 68 09 08    	mov    0x80968ac,%esi
 805d6bf:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d6c5:	6b c0 32             	imul   $0x32,%eax,%eax
 805d6c8:	c1 e0 02             	shl    $0x2,%eax
 805d6cb:	8d 14 c5 00 00 00 00 	lea    0x0(,%eax,8),%edx
 805d6d2:	29 c2                	sub    %eax,%edx
 805d6d4:	8d 42 70             	lea    0x70(%edx),%eax
 805d6d7:	01 f0                	add    %esi,%eax
 805d6d9:	83 c0 04             	add    $0x4,%eax
 805d6dc:	89 5c 24 08          	mov    %ebx,0x8(%esp)
 805d6e0:	89 4c 24 04          	mov    %ecx,0x4(%esp)
 805d6e4:	89 04 24             	mov    %eax,(%esp)
 805d6e7:	e8 c4 b2 ff ff       	call   80589b0 <memcpy@plt>
 805d6ec:	8b 85 04 fc ff ff    	mov    -0x3fc(%ebp),%eax
 805d6f2:	8d 14 85 00 00 00 00 	lea    0x0(,%eax,4),%edx
 805d6f9:	89 d0                	mov    %edx,%eax
 805d6fb:	c1 e0 03             	shl    $0x3,%eax
 805d6fe:	29 d0                	sub    %edx,%eax
 805d700:	89 c2                	mov    %eax,%edx
 805d702:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d708:	01 d0                	add    %edx,%eax
 805d70a:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d710:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d716:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d71c:	01 d0                	add    %edx,%eax
 805d71e:	83 c0 10             	add    $0x10,%eax
 805d721:	0f b6 00             	movzbl (%eax),%eax
 805d724:	84 c0                	test   %al,%al
 805d726:	75 2d                	jne    805d755 <DoRtUDP()+0x2d89>
 805d728:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d72e:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d734:	01 d0                	add    %edx,%eax
 805d736:	83 c0 10             	add    $0x10,%eax
 805d739:	c6 00 01             	movb   $0x1,(%eax)
 805d73c:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d741:	8b 15 ac 68 09 08    	mov    0x80968ac,%edx
 805d747:	8b 4a 08             	mov    0x8(%edx),%ecx
 805d74a:	8b 95 04 fc ff ff    	mov    -0x3fc(%ebp),%edx
 805d750:	01 ca                	add    %ecx,%edx
 805d752:	89 50 08             	mov    %edx,0x8(%eax)
 805d755:	8b 85 94 fc ff ff    	mov    -0x36c(%ebp),%eax
 805d75b:	89 44 24 04          	mov    %eax,0x4(%esp)
 805d75f:	c7 04 24 0c ad 08 08 	movl   $0x808ad0c,(%esp)
 805d766:	e8 05 b3 ff ff       	call   8058a70 <printf@plt>
 805d76b:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d770:	8b 50 08             	mov    0x8(%eax),%edx
 805d773:	a1 ac 68 09 08       	mov    0x80968ac,%eax
 805d778:	8b 00                	mov    (%eax),%eax
 805d77a:	39 c2                	cmp    %eax,%edx
 805d77c:	75 18                	jne    805d796 <DoRtUDP()+0x2dca>
 805d77e:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d783:	c6 80 67 05 00 00 02 	movb   $0x2,0x567(%eax)
 805d78a:	c7 04 24 4c ad 08 08 	movl   $0x808ad4c,(%esp)
 805d791:	e8 8a b4 ff ff       	call   8058c20 <puts@plt>
 805d796:	e9 a0 02 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d79b:	81 bd 24 fc ff ff d6 	cmpl   $0xd6,-0x3dc(%ebp)
 805d7a2:	00 00 00 
 805d7a5:	75 31                	jne    805d7d8 <DoRtUDP()+0x2e0c>
 805d7a7:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d7ac:	0f b6 80 67 05 00 00 	movzbl 0x567(%eax),%eax
 805d7b3:	3c 02                	cmp    $0x2,%al
 805d7b5:	0f 85 80 02 00 00    	jne    805da3b <DoRtUDP()+0x306f>
 805d7bb:	c7 04 24 7c ad 08 08 	movl   $0x808ad7c,(%esp)
 805d7c2:	e8 59 b4 ff ff       	call   8058c20 <puts@plt>
 805d7c7:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d7cc:	c6 80 67 05 00 00 03 	movb   $0x3,0x567(%eax)
 805d7d3:	e9 63 02 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d7d8:	81 bd 24 fc ff ff dd 	cmpl   $0xdd,-0x3dc(%ebp)
 805d7df:	00 00 00 
 805d7e2:	75 48                	jne    805d82c <DoRtUDP()+0x2e60>
 805d7e4:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d7ea:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d7f0:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d7f5:	8b 00                	mov    (%eax),%eax
 805d7f7:	89 82 c8 02 00 00    	mov    %eax,0x2c8(%edx)
 805d7fd:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d803:	83 c0 04             	add    $0x4,%eax
 805d806:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d80c:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d811:	d9 80 c8 02 00 00    	flds   0x2c8(%eax)
 805d817:	dd 5c 24 04          	fstpl  0x4(%esp)
 805d81b:	c7 04 24 b0 ad 08 08 	movl   $0x808adb0,(%esp)
 805d822:	e8 49 b2 ff ff       	call   8058a70 <printf@plt>
 805d827:	e9 0f 02 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d82c:	81 bd 24 fc ff ff d7 	cmpl   $0xd7,-0x3dc(%ebp)
 805d833:	00 00 00 
 805d836:	75 31                	jne    805d869 <DoRtUDP()+0x2e9d>
 805d838:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d83d:	0f b6 80 67 05 00 00 	movzbl 0x567(%eax),%eax
 805d844:	3c 02                	cmp    $0x2,%al
 805d846:	0f 8e ef 01 00 00    	jle    805da3b <DoRtUDP()+0x306f>
 805d84c:	c7 04 24 d4 ad 08 08 	movl   $0x808add4,(%esp)
 805d853:	e8 c8 b3 ff ff       	call   8058c20 <puts@plt>
 805d858:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d85d:	c6 80 67 05 00 00 09 	movb   $0x9,0x567(%eax)
 805d864:	e9 d2 01 00 00       	jmp    805da3b <DoRtUDP()+0x306f>
 805d869:	81 bd 24 fc ff ff 96 	cmpl   $0x96,-0x3dc(%ebp)
 805d870:	00 00 00 
 805d873:	0f 85 7c 01 00 00    	jne    805d9f5 <DoRtUDP()+0x3029>
 805d879:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d87f:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d884:	89 85 98 fc ff ff    	mov    %eax,-0x368(%ebp)
 805d88a:	ba 1e 00 00 00       	mov    $0x1e,%edx
 805d88f:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d895:	01 d0                	add    %edx,%eax
 805d897:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d89d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d8a3:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d8a8:	89 85 9c fc ff ff    	mov    %eax,-0x364(%ebp)
 805d8ae:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d8b4:	83 c0 01             	add    $0x1,%eax
 805d8b7:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d8bd:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d8c3:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d8c8:	89 85 a0 fc ff ff    	mov    %eax,-0x360(%ebp)
 805d8ce:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d8d4:	83 c0 01             	add    $0x1,%eax
 805d8d7:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d8dd:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d8e3:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d8e8:	89 85 a4 fc ff ff    	mov    %eax,-0x35c(%ebp)
 805d8ee:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d8f4:	83 c0 04             	add    $0x4,%eax
 805d8f7:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d8fd:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d903:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d908:	89 85 a8 fc ff ff    	mov    %eax,-0x358(%ebp)
 805d90e:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d914:	83 c0 04             	add    $0x4,%eax
 805d917:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d91d:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d923:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805d928:	89 85 ac fc ff ff    	mov    %eax,-0x354(%ebp)
 805d92e:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805d934:	83 c0 02             	add    $0x2,%eax
 805d937:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805d93d:	c7 85 08 fc ff ff 00 	movl   $0x0,-0x3f8(%ebp)
 805d944:	00 00 00 
 805d947:	eb 2f                	jmp    805d978 <DoRtUDP()+0x2fac>
 805d949:	8b 0d a4 68 09 08    	mov    0x80968a4,%ecx
 805d94f:	8b 95 08 fc ff ff    	mov    -0x3f8(%ebp),%edx
 805d955:	8b 85 98 fc ff ff    	mov    -0x368(%ebp),%eax
 805d95b:	01 d0                	add    %edx,%eax
 805d95d:	0f b6 00             	movzbl (%eax),%eax
 805d960:	8b 95 08 fc ff ff    	mov    -0x3f8(%ebp),%edx
 805d966:	01 ca                	add    %ecx,%edx
 805d968:	81 c2 60 05 00 00    	add    $0x560,%edx
 805d96e:	88 42 08             	mov    %al,0x8(%edx)
 805d971:	83 85 08 fc ff ff 01 	addl   $0x1,-0x3f8(%ebp)
 805d978:	83 bd 08 fc ff ff 1d 	cmpl   $0x1d,-0x3f8(%ebp)
 805d97f:	7e c8                	jle    805d949 <DoRtUDP()+0x2f7d>
 805d981:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d986:	8b 95 9c fc ff ff    	mov    -0x364(%ebp),%edx
 805d98c:	0f b6 12             	movzbl (%edx),%edx
 805d98f:	88 90 86 05 00 00    	mov    %dl,0x586(%eax)
 805d995:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d99a:	8b 95 a0 fc ff ff    	mov    -0x360(%ebp),%edx
 805d9a0:	0f b6 12             	movzbl (%edx),%edx
 805d9a3:	88 90 87 05 00 00    	mov    %dl,0x587(%eax)
 805d9a9:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d9ae:	8b 95 a4 fc ff ff    	mov    -0x35c(%ebp),%edx
 805d9b4:	8b 12                	mov    (%edx),%edx
 805d9b6:	89 90 88 05 00 00    	mov    %edx,0x588(%eax)
 805d9bc:	8b 15 a4 68 09 08    	mov    0x80968a4,%edx
 805d9c2:	8b 85 a8 fc ff ff    	mov    -0x358(%ebp),%eax
 805d9c8:	8b 00                	mov    (%eax),%eax
 805d9ca:	89 82 8c 05 00 00    	mov    %eax,0x58c(%edx)
 805d9d0:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d9d5:	8b 95 ac fc ff ff    	mov    -0x354(%ebp),%edx
 805d9db:	0f b7 12             	movzwl (%edx),%edx
 805d9de:	66 89 90 90 05 00 00 	mov    %dx,0x590(%eax)
 805d9e5:	a1 a4 68 09 08       	mov    0x80968a4,%eax
 805d9ea:	66 c7 80 92 05 00 00 	movw   $0x0,0x592(%eax)
 805d9f1:	00 00 
 805d9f3:	eb 46                	jmp    805da3b <DoRtUDP()+0x306f>
 805d9f5:	81 bd 24 fc ff ff fb 	cmpl   $0xfb,-0x3dc(%ebp)
 805d9fc:	00 00 00 
 805d9ff:	75 22                	jne    805da23 <DoRtUDP()+0x3057>
 805da01:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805da07:	05 e0 62 09 08       	add    $0x80962e0,%eax
 805da0c:	89 85 b0 fc ff ff    	mov    %eax,-0x350(%ebp)
 805da12:	8b 85 f0 fb ff ff    	mov    -0x410(%ebp),%eax
 805da18:	83 c0 01             	add    $0x1,%eax
 805da1b:	89 85 f0 fb ff ff    	mov    %eax,-0x410(%ebp)
 805da21:	eb 18                	jmp    805da3b <DoRtUDP()+0x306f>
 805da23:	8b 85 24 fc ff ff    	mov    -0x3dc(%ebp),%eax
 805da29:	89 44 24 04          	mov    %eax,0x4(%esp)
 805da2d:	c7 04 24 07 ae 08 08 	movl   $0x808ae07,(%esp)
 805da34:	e8 37 b0 ff ff       	call   8058a70 <printf@plt>
 805da39:	eb 1a                	jmp    805da55 <DoRtUDP()+0x3089>
 805da3b:	83 85 f8 fb ff ff 01 	addl   $0x1,-0x408(%ebp)
 805da42:	0f b6 85 e4 fb ff ff 	movzbl -0x41c(%ebp),%eax
 805da49:	3b 85 f8 fb ff ff    	cmp    -0x408(%ebp),%eax
 805da4f:	0f 8f d0 d3 ff ff    	jg     805ae25 <DoRtUDP()+0x459>
 805da55:	a1 d8 62 09 08       	mov    0x80962d8,%eax
 805da5a:	83 f8 01             	cmp    $0x1,%eax
 805da5d:	0f 85 2d 04 00 00    	jne    805de90 <DoRtUDP()+0x34c4>
 805da63:	83 bd e8 fb ff ff 00 	cmpl   $0x0,-0x418(%ebp)
 805da6a:	75 16                	jne    805da82 <DoRtUDP()+0x30b6>
 805da6c:	a1 40 62 09 08       	mov    0x8096240,%eax
 805da71:	a3 b4 68 09 08       	mov    %eax,0x80968b4
 805da76:	c7 05 b8 68 09 08 00 	movl   $0x0,0x80968b8
 805da7d:	00 00 00 
 805da80:	eb 22                	jmp    805daa4 <DoRtUDP()+0x30d8>
 805da82:	83 bd 0c fc ff ff 00 	cmpl   $0x0,-0x3f4(%ebp)
 805da89:	75 0f                	jne    805da9a <DoRtUDP()+0x30ce>
 805da8b:	a1 b8 68 09 08       	mov    0x80968b8,%eax
 805da90:	83 c0 01             	add    $0x1,%eax
 805da93:	a3 b8 68 09 08       	mov    %eax,0x80968b8
 805da98:	eb 0a                	jmp    805daa4 <DoRtUDP()+0x30d8>
 805da9a:	c7 05 b8 68 09 08 00 	movl   $0x0,0x80968b8
 805daa1:	00 00 00 
 805daa4:	c7 04 24 7a 12 00 00 	movl   $0x127a,(%esp)
 805daab:	e8 60 ad ff ff       	call   8058810 <htons@plt>
 805dab0:	66 a3 b2 62 09 08    	mov    %ax,0x80962b2
 805dab6:	a1 c0 62 09 08       	mov    0x80962c0,%eax
 805dabb:	89 c3                	mov    %eax,%ebx
 805dabd:	8b 0d 44 62 09 08    	mov    0x8096244,%ecx
 805dac3:	8b 15 a0 68 09 08    	mov    0x80968a0,%edx
 805dac9:	a1 38 62 09 08       	mov    0x8096238,%eax
 805dace:	89 5c 24 14          	mov    %ebx,0x14(%esp)
 805dad2:	c7 44 24 10 b0 62 09 	movl   $0x80962b0,0x10(%esp)
 805dad9:	08 
 805dada:	c7 44 24 0c 00 00 00 	movl   $0x0,0xc(%esp)
 805dae1:	00 
 805dae2:	89 4c 24 08          	mov    %ecx,0x8(%esp)
 805dae6:	89 54 24 04          	mov    %edx,0x4(%esp)
 805daea:	89 04 24             	mov    %eax,(%esp)
 805daed:	e8 7e ac ff ff       	call   8058770 <sendto@plt>
 805daf2:	a1 a0 68 09 08       	mov    0x80968a0,%eax
 805daf7:	83 c0 02             	add    $0x2,%eax
 805dafa:	89 85 b4 fc ff ff    	mov    %eax,-0x34c(%ebp)
 805db00:	8d 85 00 fe ff ff    	lea    -0x200(%ebp),%eax
 805db06:	83 c0 02             	add    $0x2,%eax
 805db09:	89 85 b8 fc ff ff    	mov    %eax,-0x348(%ebp)
 805db0f:	c6 85 00 fe ff ff 43 	movb   $0x43,-0x200(%ebp)
 805db16:	c6 85 01 fe ff ff 48 	movb   $0x48,-0x1ff(%ebp)
 805db1d:	c7 85 bc fc ff ff 00 	movl   $0x0,-0x344(%ebp)
 805db24:	00 00 00 
 805db27:	66 c7 85 e6 fb ff ff 	movw   $0x0,-0x41a(%ebp)
 805db2e:	00 00 
 805db30:	66 c7 85 e6 fb ff ff 	movw   $0x0,-0x41a(%ebp)
 805db37:	00 00 
 805db39:	e9 8b 01 00 00       	jmp    805dcc9 <DoRtUDP()+0x32fd>
 805db3e:	a1 a0 62 09 08       	mov    0x80962a0,%eax
 805db43:	8b 40 60             	mov    0x60(%eax),%eax
 805db46:	8b 15 a0 62 09 08    	mov    0x80962a0,%edx
 805db4c:	c7 44 24 08 04 01 00 	movl   $0x104,0x8(%esp)
 805db53:	00 
 805db54:	8d 8d fc fc ff ff    	lea    -0x304(%ebp),%ecx
 805db5a:	89 4c 24 04          	mov    %ecx,0x4(%esp)
 805db5e:	89 14 24             	mov    %edx,(%esp)
 805db61:	ff d0                	call   *%eax
 805db63:	89 85 bc fc ff ff    	mov    %eax,-0x344(%ebp)
 805db69:	83 bd bc fc ff ff 00 	cmpl   $0x0,-0x344(%ebp)
 805db70:	0f 8e 42 01 00 00    	jle    805dcb8 <DoRtUDP()+0x32ec>
 805db76:	81 bd bc fc ff ff ff 	cmpl   $0xff,-0x344(%ebp)
 805db7d:	00 00 00 
 805db80:	0f 8f 32 01 00 00    	jg     805dcb8 <DoRtUDP()+0x32ec>
 805db86:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805db8c:	c7 00 01 00 00 00    	movl   $0x1,(%eax)
 805db92:	8b 85 bc fc ff ff    	mov    -0x344(%ebp),%eax
 805db98:	8d 50 ff             	lea    -0x1(%eax),%edx
 805db9b:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dba1:	89 50 0c             	mov    %edx,0xc(%eax)
 805dba4:	8b 15 30 62 09 08    	mov    0x8096230,%edx
 805dbaa:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dbb0:	89 50 08             	mov    %edx,0x8(%eax)
 805dbb3:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dbb9:	c7 40 04 00 00 00 00 	movl   $0x0,0x4(%eax)
 805dbc0:	0f b6 85 fc fc ff ff 	movzbl -0x304(%ebp),%eax
 805dbc7:	3c 43                	cmp    $0x43,%al
 805dbc9:	75 0f                	jne    805dbda <DoRtUDP()+0x320e>
 805dbcb:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dbd1:	c7 40 04 01 00 00 00 	movl   $0x1,0x4(%eax)
 805dbd8:	eb 32                	jmp    805dc0c <DoRtUDP()+0x3240>
 805dbda:	0f b6 85 fc fc ff ff 	movzbl -0x304(%ebp),%eax
 805dbe1:	3c 41                	cmp    $0x41,%al
 805dbe3:	75 0f                	jne    805dbf4 <DoRtUDP()+0x3228>
 805dbe5:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dbeb:	c7 40 04 02 00 00 00 	movl   $0x2,0x4(%eax)
 805dbf2:	eb 18                	jmp    805dc0c <DoRtUDP()+0x3240>
 805dbf4:	0f b6 85 fc fc ff ff 	movzbl -0x304(%ebp),%eax
 805dbfb:	3c 42                	cmp    $0x42,%al
 805dbfd:	75 0d                	jne    805dc0c <DoRtUDP()+0x3240>
 805dbff:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dc05:	c7 40 04 03 00 00 00 	movl   $0x3,0x4(%eax)
 805dc0c:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dc12:	8b 40 04             	mov    0x4(%eax),%eax
 805dc15:	85 c0                	test   %eax,%eax
 805dc17:	0f 84 9b 00 00 00    	je     805dcb8 <DoRtUDP()+0x32ec>
 805dc1d:	a1 30 62 09 08       	mov    0x8096230,%eax
 805dc22:	83 c0 01             	add    $0x1,%eax
 805dc25:	a3 30 62 09 08       	mov    %eax,0x8096230
 805dc2a:	a1 30 62 09 08       	mov    0x8096230,%eax
 805dc2f:	3d 40 42 0f 00       	cmp    $0xf4240,%eax
 805dc34:	7e 0a                	jle    805dc40 <DoRtUDP()+0x3274>
 805dc36:	c7 05 30 62 09 08 01 	movl   $0x1,0x8096230
 805dc3d:	00 00 00 
 805dc40:	8b 85 bc fc ff ff    	mov    -0x344(%ebp),%eax
 805dc46:	83 e8 01             	sub    $0x1,%eax
 805dc49:	8b 95 b8 fc ff ff    	mov    -0x348(%ebp),%edx
 805dc4f:	83 c2 10             	add    $0x10,%edx
 805dc52:	89 44 24 08          	mov    %eax,0x8(%esp)
 805dc56:	8d 85 fc fc ff ff    	lea    -0x304(%ebp),%eax
 805dc5c:	83 c0 01             	add    $0x1,%eax
 805dc5f:	89 44 24 04          	mov    %eax,0x4(%esp)
 805dc63:	89 14 24             	mov    %edx,(%esp)
 805dc66:	e8 45 ad ff ff       	call   80589b0 <memcpy@plt>
 805dc6b:	c7 04 24 7a 12 00 00 	movl   $0x127a,(%esp)
 805dc72:	e8 99 ab ff ff       	call   8058810 <htons@plt>
 805dc77:	66 a3 b2 62 09 08    	mov    %ax,0x80962b2
 805dc7d:	a1 c0 62 09 08       	mov    0x80962c0,%eax
 805dc82:	89 c1                	mov    %eax,%ecx
 805dc84:	ba 12 01 00 00       	mov    $0x112,%edx
 805dc89:	a1 38 62 09 08       	mov    0x8096238,%eax
 805dc8e:	89 4c 24 14          	mov    %ecx,0x14(%esp)
 805dc92:	c7 44 24 10 b0 62 09 	movl   $0x80962b0,0x10(%esp)
 805dc99:	08 
 805dc9a:	c7 44 24 0c 00 00 00 	movl   $0x0,0xc(%esp)
 805dca1:	00 
 805dca2:	89 54 24 08          	mov    %edx,0x8(%esp)
 805dca6:	8d 95 00 fe ff ff    	lea    -0x200(%ebp),%edx
 805dcac:	89 54 24 04          	mov    %edx,0x4(%esp)
 805dcb0:	89 04 24             	mov    %eax,(%esp)
 805dcb3:	e8 b8 aa ff ff       	call   8058770 <sendto@plt>
 805dcb8:	0f b7 85 e6 fb ff ff 	movzwl -0x41a(%ebp),%eax
 805dcbf:	83 c0 01             	add    $0x1,%eax
 805dcc2:	66 89 85 e6 fb ff ff 	mov    %ax,-0x41a(%ebp)
 805dcc9:	66 83 bd e6 fb ff ff 	cmpw   $0x5,-0x41a(%ebp)
 805dcd0:	05 
 805dcd1:	7f 0d                	jg     805dce0 <DoRtUDP()+0x3314>
 805dcd3:	a1 a0 62 09 08       	mov    0x80962a0,%eax
 805dcd8:	85 c0                	test   %eax,%eax
 805dcda:	0f 85 5e fe ff ff    	jne    805db3e <DoRtUDP()+0x3172>
 805dce0:	66 c7 85 e6 fb ff ff 	movw   $0x0,-0x41a(%ebp)
 805dce7:	00 00 
 805dce9:	e9 8b 01 00 00       	jmp    805de79 <DoRtUDP()+0x34ad>
 805dcee:	a1 a4 62 09 08       	mov    0x80962a4,%eax
 805dcf3:	8b 40 60             	mov    0x60(%eax),%eax
 805dcf6:	8b 15 a4 62 09 08    	mov    0x80962a4,%edx
 805dcfc:	c7 44 24 08 04 01 00 	movl   $0x104,0x8(%esp)
 805dd03:	00 
 805dd04:	8d 8d fc fc ff ff    	lea    -0x304(%ebp),%ecx
 805dd0a:	89 4c 24 04          	mov    %ecx,0x4(%esp)
 805dd0e:	89 14 24             	mov    %edx,(%esp)
 805dd11:	ff d0                	call   *%eax
 805dd13:	89 85 bc fc ff ff    	mov    %eax,-0x344(%ebp)
 805dd19:	83 bd bc fc ff ff 00 	cmpl   $0x0,-0x344(%ebp)
 805dd20:	0f 8e 42 01 00 00    	jle    805de68 <DoRtUDP()+0x349c>
 805dd26:	81 bd bc fc ff ff ff 	cmpl   $0xff,-0x344(%ebp)
 805dd2d:	00 00 00 
 805dd30:	0f 8f 32 01 00 00    	jg     805de68 <DoRtUDP()+0x349c>
 805dd36:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dd3c:	c7 00 02 00 00 00    	movl   $0x2,(%eax)
 805dd42:	8b 85 bc fc ff ff    	mov    -0x344(%ebp),%eax
 805dd48:	8d 50 ff             	lea    -0x1(%eax),%edx
 805dd4b:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dd51:	89 50 0c             	mov    %edx,0xc(%eax)
 805dd54:	8b 15 34 62 09 08    	mov    0x8096234,%edx
 805dd5a:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dd60:	89 50 08             	mov    %edx,0x8(%eax)
 805dd63:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dd69:	c7 40 04 00 00 00 00 	movl   $0x0,0x4(%eax)
 805dd70:	0f b6 85 fc fc ff ff 	movzbl -0x304(%ebp),%eax
 805dd77:	3c 43                	cmp    $0x43,%al
 805dd79:	75 0f                	jne    805dd8a <DoRtUDP()+0x33be>
 805dd7b:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dd81:	c7 40 04 01 00 00 00 	movl   $0x1,0x4(%eax)
 805dd88:	eb 32                	jmp    805ddbc <DoRtUDP()+0x33f0>
 805dd8a:	0f b6 85 fc fc ff ff 	movzbl -0x304(%ebp),%eax
 805dd91:	3c 41                	cmp    $0x41,%al
 805dd93:	75 0f                	jne    805dda4 <DoRtUDP()+0x33d8>
 805dd95:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805dd9b:	c7 40 04 02 00 00 00 	movl   $0x2,0x4(%eax)
 805dda2:	eb 18                	jmp    805ddbc <DoRtUDP()+0x33f0>
 805dda4:	0f b6 85 fc fc ff ff 	movzbl -0x304(%ebp),%eax
 805ddab:	3c 42                	cmp    $0x42,%al
 805ddad:	75 0d                	jne    805ddbc <DoRtUDP()+0x33f0>
 805ddaf:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805ddb5:	c7 40 04 03 00 00 00 	movl   $0x3,0x4(%eax)
 805ddbc:	8b 85 b8 fc ff ff    	mov    -0x348(%ebp),%eax
 805ddc2:	8b 40 04             	mov    0x4(%eax),%eax
 805ddc5:	85 c0                	test   %eax,%eax
 805ddc7:	0f 84 9b 00 00 00    	je     805de68 <DoRtUDP()+0x349c>
 805ddcd:	a1 34 62 09 08       	mov    0x8096234,%eax
 805ddd2:	83 c0 01             	add    $0x1,%eax
 805ddd5:	a3 34 62 09 08       	mov    %eax,0x8096234
 805ddda:	a1 34 62 09 08       	mov    0x8096234,%eax
 805dddf:	3d 40 42 0f 00       	cmp    $0xf4240,%eax
 805dde4:	7e 0a                	jle    805ddf0 <DoRtUDP()+0x3424>
 805dde6:	c7 05 34 62 09 08 01 	movl   $0x1,0x8096234
 805dded:	00 00 00 
 805ddf0:	8b 85 bc fc ff ff    	mov    -0x344(%ebp),%eax
 805ddf6:	83 e8 01             	sub    $0x1,%eax
 805ddf9:	8b 95 b8 fc ff ff    	mov    -0x348(%ebp),%edx
 805ddff:	83 c2 10             	add    $0x10,%edx
 805de02:	89 44 24 08          	mov    %eax,0x8(%esp)
 805de06:	8d 85 fc fc ff ff    	lea    -0x304(%ebp),%eax
 805de0c:	83 c0 01             	add    $0x1,%eax
 805de0f:	89 44 24 04          	mov    %eax,0x4(%esp)
 805de13:	89 14 24             	mov    %edx,(%esp)
 805de16:	e8 95 ab ff ff       	call   80589b0 <memcpy@plt>
 805de1b:	c7 04 24 7a 12 00 00 	movl   $0x127a,(%esp)
 805de22:	e8 e9 a9 ff ff       	call   8058810 <htons@plt>
 805de27:	66 a3 b2 62 09 08    	mov    %ax,0x80962b2
 805de2d:	a1 c0 62 09 08       	mov    0x80962c0,%eax
 805de32:	89 c1                	mov    %eax,%ecx
 805de34:	ba 12 01 00 00       	mov    $0x112,%edx
 805de39:	a1 38 62 09 08       	mov    0x8096238,%eax
 805de3e:	89 4c 24 14          	mov    %ecx,0x14(%esp)
 805de42:	c7 44 24 10 b0 62 09 	movl   $0x80962b0,0x10(%esp)
 805de49:	08 
 805de4a:	c7 44 24 0c 00 00 00 	movl   $0x0,0xc(%esp)
 805de51:	00 
 805de52:	89 54 24 08          	mov    %edx,0x8(%esp)
 805de56:	8d 95 00 fe ff ff    	lea    -0x200(%ebp),%edx
 805de5c:	89 54 24 04          	mov    %edx,0x4(%esp)
 805de60:	89 04 24             	mov    %eax,(%esp)
 805de63:	e8 08 a9 ff ff       	call   8058770 <sendto@plt>
 805de68:	0f b7 85 e6 fb ff ff 	movzwl -0x41a(%ebp),%eax
 805de6f:	83 c0 01             	add    $0x1,%eax
 805de72:	66 89 85 e6 fb ff ff 	mov    %ax,-0x41a(%ebp)
 805de79:	66 83 bd e6 fb ff ff 	cmpw   $0x5,-0x41a(%ebp)
 805de80:	05 
 805de81:	7f 0d                	jg     805de90 <DoRtUDP()+0x34c4>
 805de83:	a1 a4 62 09 08       	mov    0x80962a4,%eax
 805de88:	85 c0                	test   %eax,%eax
 805de8a:	0f 85 5e fe ff ff    	jne    805dcee <DoRtUDP()+0x3322>
 805de90:	8b 45 f4             	mov    -0xc(%ebp),%eax
 805de93:	65 33 05 14 00 00 00 	xor    %gs:0x14,%eax
 805de9a:	74 05                	je     805dea1 <DoRtUDP()+0x34d5>
 805de9c:	e8 cf ac ff ff       	call   8058b70 <__stack_chk_fail@plt>
 805dea1:	81 c4 40 04 00 00    	add    $0x440,%esp
 805dea7:	5b                   	pop    %ebx
 805dea8:	5e                   	pop    %esi
 805dea9:	5d                   	pop    %ebp
 805deaa:	c3                   	ret    
