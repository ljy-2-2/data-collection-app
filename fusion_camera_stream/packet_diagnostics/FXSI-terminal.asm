
fusion_camera_stream/packet_diagnostics/FXSI.reference.ko：     文件格式 elf32-i386


Disassembly of section .text:

000015b0 <_FX_UpdateTerminal>:
    15b0:	55                   	push   %ebp
    15b1:	89 e5                	mov    %esp,%ebp
    15b3:	57                   	push   %edi
    15b4:	56                   	push   %esi
    15b5:	89 d6                	mov    %edx,%esi
    15b7:	89 c2                	mov    %eax,%edx
    15b9:	53                   	push   %ebx
    15ba:	c1 e2 0a             	shl    $0xa,%edx
    15bd:	89 c3                	mov    %eax,%ebx
    15bf:	8d 3c 16             	lea    (%esi,%edx,1),%edi
    15c2:	83 ec 14             	sub    $0x14,%esp
    15c5:	0f b6 87 24 44 00 00 	movzbl 0x4424(%edi),%eax
    15cc:	84 c0                	test   %al,%al
    15ce:	0f 84 cc 00 00 00    	je     16a0 <_FX_UpdateTerminal+0xf0>
    15d4:	3b 04 9d 00 00 00 00 	cmp    0x0(,%ebx,4),%eax
			15d7: R_386_32	m_canFD_Recv_data_len
    15db:	a3 c8 02 00 00       	mov    %eax,0x2c8
			15dc: R_386_32	.bss
    15e0:	0f 84 fa 01 00 00    	je     17e0 <_FX_UpdateTerminal+0x230>
    15e6:	0f b6 8f 23 44 00 00 	movzbl 0x4423(%edi),%ecx
    15ed:	8d 94 16 20 44 00 00 	lea    0x4420(%esi,%edx,1),%edx
    15f4:	88 8f 24 44 00 00    	mov    %cl,0x4424(%edi)
    15fa:	0f b6 8f 22 44 00 00 	movzbl 0x4422(%edi),%ecx
    1601:	88 8f 23 44 00 00    	mov    %cl,0x4423(%edi)
    1607:	0f b6 8f 21 44 00 00 	movzbl 0x4421(%edi),%ecx
    160e:	88 8f 22 44 00 00    	mov    %cl,0x4422(%edi)
    1614:	0f b6 8f 20 44 00 00 	movzbl 0x4420(%edi),%ecx
    161b:	c6 87 20 44 00 00 43 	movb   $0x43,0x4420(%edi)
    1622:	88 8f 21 44 00 00    	mov    %cl,0x4421(%edi)
    1628:	8d 48 05             	lea    0x5(%eax),%ecx
    162b:	89 d8                	mov    %ebx,%eax
    162d:	e8 fc ff ff ff       	call   162e <_FX_UpdateTerminal+0x7e>
			162e: R_386_PC32	FX_TerminalRxWrite
    1632:	85 c0                	test   %eax,%eax
    1634:	0f 88 89 03 00 00    	js     19c3 <_FX_UpdateTerminal+0x413>
    163a:	a1 c8 02 00 00       	mov    0x2c8,%eax
			163b: R_386_32	.bss
    163f:	89 da                	mov    %ebx,%edx
    1641:	c7 05 c4 02 00 00 00 	movl   $0x0,0x2c4
    1648:	00 00 00 
			1643: R_386_32	.bss
    164b:	c1 e2 0a             	shl    $0xa,%edx
    164e:	01 f2                	add    %esi,%edx
    1650:	c6 82 24 44 00 00 00 	movb   $0x0,0x4424(%edx)
    1657:	8d 48 05             	lea    0x5(%eax),%ecx
    165a:	66 0f 6e c0          	movd   %eax,%xmm0
    165e:	85 c9                	test   %ecx,%ecx
    1660:	7e 35                	jle    1697 <_FX_UpdateTerminal+0xe7>
    1662:	b8 64 00 00 00       	mov    $0x64,%eax
    1667:	89 df                	mov    %ebx,%edi
    1669:	66 0f 6e cb          	movd   %ebx,%xmm1
    166d:	0f af f8             	imul   %eax,%edi
    1670:	31 c0                	xor    %eax,%eax
    1672:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi
    1678:	0f b6 9c 02 20 44 00 	movzbl 0x4420(%edx,%eax,1),%ebx
    167f:	00 
    1680:	88 9c 07 00 00 00 00 	mov    %bl,0x0(%edi,%eax,1)
			1683: R_386_32	m_canFD_Recv_data
    1687:	83 c0 01             	add    $0x1,%eax
    168a:	39 c8                	cmp    %ecx,%eax
    168c:	75 ea                	jne    1678 <_FX_UpdateTerminal+0xc8>
    168e:	66 0f 7e cb          	movd   %xmm1,%ebx
    1692:	a3 c4 02 00 00       	mov    %eax,0x2c4
			1693: R_386_32	.bss
    1697:	66 0f 7e 04 9d 00 00 	movd   %xmm0,0x0(,%ebx,4)
    169e:	00 00 
			169c: R_386_32	m_canFD_Recv_data_len
    16a0:	89 d8                	mov    %ebx,%eax
    16a2:	c1 e0 0a             	shl    $0xa,%eax
    16a5:	8d 94 06 60 44 00 00 	lea    0x4460(%esi,%eax,1),%edx
    16ac:	0f b6 4a 05          	movzbl 0x5(%edx),%ecx
    16b0:	84 c9                	test   %cl,%cl
    16b2:	0f 85 00 02 00 00    	jne    18b8 <_FX_UpdateTerminal+0x308>
    16b8:	89 d8                	mov    %ebx,%eax
    16ba:	c1 e0 0a             	shl    $0xa,%eax
    16bd:	8d 94 06 a0 44 00 00 	lea    0x44a0(%esi,%eax,1),%edx
    16c4:	0f b6 4a 06          	movzbl 0x6(%edx),%ecx
    16c8:	84 c9                	test   %cl,%cl
    16ca:	0f 85 b0 01 00 00    	jne    1880 <_FX_UpdateTerminal+0x2d0>
    16d0:	89 df                	mov    %ebx,%edi
    16d2:	c1 e7 0a             	shl    $0xa,%edi
    16d5:	8d 04 3e             	lea    (%esi,%edi,1),%eax
    16d8:	c6 80 65 4c 00 00 00 	movb   $0x0,0x4c65(%eax)
    16df:	c6 80 a6 4c 00 00 00 	movb   $0x0,0x4ca6(%eax)
    16e6:	8d 87 a0 4c 00 00    	lea    0x4ca0(%edi),%eax
    16ec:	89 45 f0             	mov    %eax,-0x10(%ebp)
    16ef:	90                   	nop
    16f0:	b9 64 00 00 00       	mov    $0x64,%ecx
    16f5:	ba 60 02 00 00       	mov    $0x260,%edx
			16f6: R_386_32	.bss
    16fa:	89 d8                	mov    %ebx,%eax
    16fc:	e8 fc ff ff ff       	call   16fd <_FX_UpdateTerminal+0x14d>
			16fd: R_386_PC32	FX_TerminalTxRead
    1701:	85 c0                	test   %eax,%eax
    1703:	a3 c8 02 00 00       	mov    %eax,0x2c8
			1704: R_386_32	.bss
    1708:	0f 8e c2 00 00 00    	jle    17d0 <_FX_UpdateTerminal+0x220>
    170e:	0f b6 15 60 02 00 00 	movzbl 0x260,%edx
			1711: R_386_32	.bss
    1715:	80 fa 43             	cmp    $0x43,%dl
    1718:	74 36                	je     1750 <_FX_UpdateTerminal+0x1a0>
    171a:	80 fa 41             	cmp    $0x41,%dl
    171d:	0f 84 85 00 00 00    	je     17a8 <_FX_UpdateTerminal+0x1f8>
    1723:	80 fa 42             	cmp    $0x42,%dl
    1726:	75 c8                	jne    16f0 <_FX_UpdateTerminal+0x140>
    1728:	8d 50 ff             	lea    -0x1(%eax),%edx
    172b:	89 c1                	mov    %eax,%ecx
    172d:	8b 45 f0             	mov    -0x10(%ebp),%eax
    1730:	88 15 60 02 00 00    	mov    %dl,0x260
			1732: R_386_32	.bss
    1736:	ba 60 02 00 00       	mov    $0x260,%edx
			1737: R_386_32	.bss
    173b:	8d 44 06 06          	lea    0x6(%esi,%eax,1),%eax
    173f:	e8 fc ff ff ff       	call   1740 <_FX_UpdateTerminal+0x190>
			1740: R_386_PC32	memcpy
    1744:	eb aa                	jmp    16f0 <_FX_UpdateTerminal+0x140>
    1746:	8d 76 00             	lea    0x0(%esi),%esi
    1749:	8d bc 27 00 00 00 00 	lea    0x0(%edi,%eiz,1),%edi
    1750:	0f b6 15 61 02 00 00 	movzbl 0x261,%edx
			1753: R_386_32	.bss
    1757:	89 c1                	mov    %eax,%ecx
    1759:	88 15 60 02 00 00    	mov    %dl,0x260
			175b: R_386_32	.bss
    175f:	0f b6 15 62 02 00 00 	movzbl 0x262,%edx
			1762: R_386_32	.bss
    1766:	88 15 61 02 00 00    	mov    %dl,0x261
			1768: R_386_32	.bss
    176c:	0f b6 15 63 02 00 00 	movzbl 0x263,%edx
			176f: R_386_32	.bss
    1773:	88 15 62 02 00 00    	mov    %dl,0x262
			1775: R_386_32	.bss
    1779:	0f b6 15 64 02 00 00 	movzbl 0x264,%edx
			177c: R_386_32	.bss
    1780:	88 15 63 02 00 00    	mov    %dl,0x263
			1782: R_386_32	.bss
    1786:	8d 50 fb             	lea    -0x5(%eax),%edx
    1789:	88 15 64 02 00 00    	mov    %dl,0x264
			178b: R_386_32	.bss
    178f:	8d 84 3e 20 4c 00 00 	lea    0x4c20(%esi,%edi,1),%eax
    1796:	ba 60 02 00 00       	mov    $0x260,%edx
			1797: R_386_32	.bss
    179b:	e8 fc ff ff ff       	call   179c <_FX_UpdateTerminal+0x1ec>
			179c: R_386_PC32	memcpy
    17a0:	e9 4b ff ff ff       	jmp    16f0 <_FX_UpdateTerminal+0x140>
    17a5:	8d 76 00             	lea    0x0(%esi),%esi
    17a8:	8d 50 ff             	lea    -0x1(%eax),%edx
    17ab:	89 c1                	mov    %eax,%ecx
    17ad:	88 15 60 02 00 00    	mov    %dl,0x260
			17af: R_386_32	.bss
    17b3:	8d 84 3e 65 4c 00 00 	lea    0x4c65(%esi,%edi,1),%eax
    17ba:	ba 60 02 00 00       	mov    $0x260,%edx
			17bb: R_386_32	.bss
    17bf:	e8 fc ff ff ff       	call   17c0 <_FX_UpdateTerminal+0x210>
			17c0: R_386_PC32	memcpy
    17c4:	e9 27 ff ff ff       	jmp    16f0 <_FX_UpdateTerminal+0x140>
    17c9:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    17d0:	83 c4 14             	add    $0x14,%esp
    17d3:	5b                   	pop    %ebx
    17d4:	5e                   	pop    %esi
    17d5:	5f                   	pop    %edi
    17d6:	5d                   	pop    %ebp
    17d7:	c3                   	ret    
    17d8:	90                   	nop
    17d9:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    17e0:	0f b6 97 23 44 00 00 	movzbl 0x4423(%edi),%edx
    17e7:	8d 48 05             	lea    0x5(%eax),%ecx
    17ea:	89 d8                	mov    %ebx,%eax
    17ec:	c7 05 c4 02 00 00 00 	movl   $0x0,0x2c4
    17f3:	00 00 00 
			17ee: R_386_32	.bss
    17f6:	66 0f 6e c3          	movd   %ebx,%xmm0
    17fa:	88 97 24 44 00 00    	mov    %dl,0x4424(%edi)
    1800:	0f b6 97 22 44 00 00 	movzbl 0x4422(%edi),%edx
    1807:	88 97 23 44 00 00    	mov    %dl,0x4423(%edi)
    180d:	0f b6 97 21 44 00 00 	movzbl 0x4421(%edi),%edx
    1814:	88 97 22 44 00 00    	mov    %dl,0x4422(%edi)
    181a:	0f b6 97 20 44 00 00 	movzbl 0x4420(%edi),%edx
    1821:	c6 87 20 44 00 00 43 	movb   $0x43,0x4420(%edi)
    1828:	88 97 21 44 00 00    	mov    %dl,0x4421(%edi)
    182e:	ba 64 00 00 00       	mov    $0x64,%edx
    1833:	0f af c2             	imul   %edx,%eax
    1836:	ba 43 00 00 00       	mov    $0x43,%edx
    183b:	89 45 f0             	mov    %eax,-0x10(%ebp)
    183e:	31 c0                	xor    %eax,%eax
    1840:	8b 5d f0             	mov    -0x10(%ebp),%ebx
    1843:	eb 14                	jmp    1859 <_FX_UpdateTerminal+0x2a9>
    1845:	8d 76 00             	lea    0x0(%esi),%esi
    1848:	80 fa 01             	cmp    $0x1,%dl
    184b:	0f 85 9c 00 00 00    	jne    18ed <_FX_UpdateTerminal+0x33d>
    1851:	0f b6 94 07 20 44 00 	movzbl 0x4420(%edi,%eax,1),%edx
    1858:	00 
    1859:	38 94 03 00 00 00 00 	cmp    %dl,0x0(%ebx,%eax,1)
			185c: R_386_32	m_canFD_Recv_data
    1860:	0f 94 c2             	sete   %dl
    1863:	83 c0 01             	add    $0x1,%eax
    1866:	39 c8                	cmp    %ecx,%eax
    1868:	75 de                	jne    1848 <_FX_UpdateTerminal+0x298>
    186a:	84 d2                	test   %dl,%dl
    186c:	66 0f 7e c3          	movd   %xmm0,%ebx
    1870:	89 0d c4 02 00 00    	mov    %ecx,0x2c4
			1872: R_386_32	.bss
    1876:	0f 85 24 fe ff ff    	jne    16a0 <_FX_UpdateTerminal+0xf0>
    187c:	eb 78                	jmp    18f6 <_FX_UpdateTerminal+0x346>
    187e:	66 90                	xchg   %ax,%ax
    1880:	89 0d c8 02 00 00    	mov    %ecx,0x2c8
			1882: R_386_32	.bss
    1886:	83 c1 01             	add    $0x1,%ecx
    1889:	c6 42 06 42          	movb   $0x42,0x6(%edx)
    188d:	8d 94 06 a6 44 00 00 	lea    0x44a6(%esi,%eax,1),%edx
    1894:	89 d8                	mov    %ebx,%eax
    1896:	e8 fc ff ff ff       	call   1897 <_FX_UpdateTerminal+0x2e7>
			1897: R_386_PC32	FX_TerminalRxWrite
    189b:	85 c0                	test   %eax,%eax
    189d:	0f 88 d4 00 00 00    	js     1977 <_FX_UpdateTerminal+0x3c7>
    18a3:	89 d8                	mov    %ebx,%eax
    18a5:	c1 e0 0a             	shl    $0xa,%eax
    18a8:	c6 84 06 a6 44 00 00 	movb   $0x0,0x44a6(%esi,%eax,1)
    18af:	00 
    18b0:	e9 1b fe ff ff       	jmp    16d0 <_FX_UpdateTerminal+0x120>
    18b5:	8d 76 00             	lea    0x0(%esi),%esi
    18b8:	89 0d c8 02 00 00    	mov    %ecx,0x2c8
			18ba: R_386_32	.bss
    18be:	83 c1 01             	add    $0x1,%ecx
    18c1:	c6 42 05 41          	movb   $0x41,0x5(%edx)
    18c5:	8d 94 06 65 44 00 00 	lea    0x4465(%esi,%eax,1),%edx
    18cc:	89 d8                	mov    %ebx,%eax
    18ce:	e8 fc ff ff ff       	call   18cf <_FX_UpdateTerminal+0x31f>
			18cf: R_386_PC32	FX_TerminalRxWrite
    18d3:	85 c0                	test   %eax,%eax
    18d5:	0f 88 c2 00 00 00    	js     199d <_FX_UpdateTerminal+0x3ed>
    18db:	89 d8                	mov    %ebx,%eax
    18dd:	c1 e0 0a             	shl    $0xa,%eax
    18e0:	c6 84 06 65 44 00 00 	movb   $0x0,0x4465(%esi,%eax,1)
    18e7:	00 
    18e8:	e9 cb fd ff ff       	jmp    16b8 <_FX_UpdateTerminal+0x108>
    18ed:	66 0f 7e c3          	movd   %xmm0,%ebx
    18f1:	a3 c4 02 00 00       	mov    %eax,0x2c4
			18f2: R_386_32	.bss
    18f6:	89 d8                	mov    %ebx,%eax
    18f8:	c1 e0 0a             	shl    $0xa,%eax
    18fb:	8d 94 06 20 44 00 00 	lea    0x4420(%esi,%eax,1),%edx
    1902:	89 d8                	mov    %ebx,%eax
    1904:	e8 fc ff ff ff       	call   1905 <_FX_UpdateTerminal+0x355>
			1905: R_386_PC32	FX_TerminalRxWrite
    1909:	85 c0                	test   %eax,%eax
    190b:	0f 88 d8 00 00 00    	js     19e9 <_FX_UpdateTerminal+0x439>
    1911:	8b 0d c8 02 00 00    	mov    0x2c8,%ecx
			1913: R_386_32	.bss
    1917:	89 d8                	mov    %ebx,%eax
    1919:	c7 05 c4 02 00 00 00 	movl   $0x0,0x2c4
    1920:	00 00 00 
			191b: R_386_32	.bss
    1923:	c1 e0 0a             	shl    $0xa,%eax
    1926:	c6 84 06 24 44 00 00 	movb   $0x0,0x4424(%esi,%eax,1)
    192d:	00 
    192e:	31 c0                	xor    %eax,%eax
    1930:	8d 51 05             	lea    0x5(%ecx),%edx
    1933:	85 d2                	test   %edx,%edx
    1935:	7e 34                	jle    196b <_FX_UpdateTerminal+0x3bb>
    1937:	66 0f 6e c1          	movd   %ecx,%xmm0
    193b:	8b 4d f0             	mov    -0x10(%ebp),%ecx
    193e:	66 0f 6e cb          	movd   %ebx,%xmm1
    1942:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi
    1948:	0f b6 9c 07 20 44 00 	movzbl 0x4420(%edi,%eax,1),%ebx
    194f:	00 
    1950:	88 9c 01 00 00 00 00 	mov    %bl,0x0(%ecx,%eax,1)
			1953: R_386_32	m_canFD_Recv_data
    1957:	83 c0 01             	add    $0x1,%eax
    195a:	39 d0                	cmp    %edx,%eax
    195c:	75 ea                	jne    1948 <_FX_UpdateTerminal+0x398>
    195e:	66 0f 7e c1          	movd   %xmm0,%ecx
    1962:	66 0f 7e cb          	movd   %xmm1,%ebx
    1966:	a3 c4 02 00 00       	mov    %eax,0x2c4
			1967: R_386_32	.bss
    196b:	89 0c 9d 00 00 00 00 	mov    %ecx,0x0(,%ebx,4)
			196e: R_386_32	m_canFD_Recv_data_len
    1972:	e9 29 fd ff ff       	jmp    16a0 <_FX_UpdateTerminal+0xf0>
    1977:	a1 c8 02 00 00       	mov    0x2c8,%eax
			1978: R_386_32	.bss
    197c:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1980:	c7 44 24 04 84 03 00 	movl   $0x384,0x4(%esp)
    1987:	00 
			1984: R_386_32	.rodata
    1988:	c7 04 24 48 07 00 00 	movl   $0x748,(%esp)
			198b: R_386_32	.rodata.str1.4
    198f:	89 44 24 0c          	mov    %eax,0xc(%esp)
    1993:	e8 fc ff ff ff       	call   1994 <_FX_UpdateTerminal+0x3e4>
			1994: R_386_PC32	WARN_LOG
    1998:	e9 06 ff ff ff       	jmp    18a3 <_FX_UpdateTerminal+0x2f3>
    199d:	a1 c8 02 00 00       	mov    0x2c8,%eax
			199e: R_386_32	.bss
    19a2:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    19a6:	c7 44 24 04 84 03 00 	movl   $0x384,0x4(%esp)
    19ad:	00 
			19aa: R_386_32	.rodata
    19ae:	c7 04 24 10 07 00 00 	movl   $0x710,(%esp)
			19b1: R_386_32	.rodata.str1.4
    19b5:	89 44 24 0c          	mov    %eax,0xc(%esp)
    19b9:	e8 fc ff ff ff       	call   19ba <_FX_UpdateTerminal+0x40a>
			19ba: R_386_PC32	WARN_LOG
    19be:	e9 18 ff ff ff       	jmp    18db <_FX_UpdateTerminal+0x32b>
    19c3:	a1 c8 02 00 00       	mov    0x2c8,%eax
			19c4: R_386_32	.bss
    19c8:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    19cc:	c7 44 24 04 84 03 00 	movl   $0x384,0x4(%esp)
    19d3:	00 
			19d0: R_386_32	.rodata
    19d4:	c7 04 24 dc 06 00 00 	movl   $0x6dc,(%esp)
			19d7: R_386_32	.rodata.str1.4
    19db:	89 44 24 0c          	mov    %eax,0xc(%esp)
    19df:	e8 fc ff ff ff       	call   19e0 <_FX_UpdateTerminal+0x430>
			19e0: R_386_PC32	WARN_LOG
    19e4:	e9 51 fc ff ff       	jmp    163a <_FX_UpdateTerminal+0x8a>
    19e9:	a1 c8 02 00 00       	mov    0x2c8,%eax
			19ea: R_386_32	.bss
    19ee:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    19f2:	c7 44 24 04 84 03 00 	movl   $0x384,0x4(%esp)
    19f9:	00 
			19f6: R_386_32	.rodata
    19fa:	c7 04 24 dc 06 00 00 	movl   $0x6dc,(%esp)
			19fd: R_386_32	.rodata.str1.4
    1a01:	89 44 24 0c          	mov    %eax,0xc(%esp)
    1a05:	e8 fc ff ff ff       	call   1a06 <_FX_UpdateTerminal+0x456>
			1a06: R_386_PC32	WARN_LOG
    1a0a:	e9 02 ff ff ff       	jmp    1911 <_FX_UpdateTerminal+0x361>
    1a0f:	90                   	nop

00001a10 <_FX_SyncDistributedClocks>:
    1a10:	55                   	push   %ebp
    1a11:	89 e5                	mov    %esp,%ebp
    1a13:	57                   	push   %edi
    1a14:	56                   	push   %esi
    1a15:	89 c6                	mov    %eax,%esi
    1a17:	53                   	push   %ebx
    1a18:	89 d3                	mov    %edx,%ebx
    1a1a:	83 e4 f8             	and    $0xfffffff8,%esp
    1a1d:	83 3c 85 40 5f 00 00 	cmpl   $0x1,0x5f40(,%eax,4)
    1a24:	01 
			1a20: R_386_32	.bss
    1a25:	74 59                	je     1a80 <_FX_SyncDistributedClocks+0x70>
    1a27:	83 3c 85 28 5f 00 00 	cmpl   $0x9,0x5f28(,%eax,4)
    1a2e:	09 
			1a2a: R_386_32	.bss
    1a2f:	7e 77                	jle    1aa8 <_FX_SyncDistributedClocks+0x98>
    1a31:	c7 04 85 40 5f 00 00 	movl   $0x1,0x5f40(,%eax,4)
    1a38:	01 00 00 00 
			1a34: R_386_32	.bss
    1a3c:	8b 55 08             	mov    0x8(%ebp),%edx
    1a3f:	8b 4d 0c             	mov    0xc(%ebp),%ecx
    1a42:	03 14 c5 30 5f 00 00 	add    0x5f30(,%eax,8),%edx
			1a45: R_386_32	.bss
    1a49:	13 0c c5 34 5f 00 00 	adc    0x5f34(,%eax,8),%ecx
			1a4c: R_386_32	.bss
    1a50:	89 14 c5 30 5f 00 00 	mov    %edx,0x5f30(,%eax,8)
			1a53: R_386_32	.bss
    1a57:	89 0c c5 34 5f 00 00 	mov    %ecx,0x5f34(,%eax,8)
			1a5a: R_386_32	.bss
    1a5e:	89 d8                	mov    %ebx,%eax
    1a60:	e8 fc ff ff ff       	call   1a61 <_FX_SyncDistributedClocks+0x51>
			1a61: R_386_PC32	ecrt_master_application_time
    1a65:	89 d8                	mov    %ebx,%eax
    1a67:	e8 fc ff ff ff       	call   1a68 <_FX_SyncDistributedClocks+0x58>
			1a68: R_386_PC32	ecrt_master_sync_reference_clock
    1a6c:	89 d8                	mov    %ebx,%eax
    1a6e:	e8 fc ff ff ff       	call   1a6f <_FX_SyncDistributedClocks+0x5f>
			1a6f: R_386_PC32	ecrt_master_sync_slave_clocks
    1a73:	8d 65 f4             	lea    -0xc(%ebp),%esp
    1a76:	5b                   	pop    %ebx
    1a77:	5e                   	pop    %esi
    1a78:	5f                   	pop    %edi
    1a79:	5d                   	pop    %ebp
    1a7a:	c3                   	ret    
    1a7b:	90                   	nop
    1a7c:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    1a80:	8b 55 08             	mov    0x8(%ebp),%edx
    1a83:	8b 4d 0c             	mov    0xc(%ebp),%ecx
    1a86:	03 14 c5 30 5f 00 00 	add    0x5f30(,%eax,8),%edx
			1a89: R_386_32	.bss
    1a8d:	13 0c c5 34 5f 00 00 	adc    0x5f34(,%eax,8),%ecx
			1a90: R_386_32	.bss
    1a94:	89 14 c5 30 5f 00 00 	mov    %edx,0x5f30(,%eax,8)
			1a97: R_386_32	.bss
    1a9b:	89 0c c5 34 5f 00 00 	mov    %ecx,0x5f34(,%eax,8)
			1a9e: R_386_32	.bss
    1aa2:	eb ba                	jmp    1a5e <_FX_SyncDistributedClocks+0x4e>
    1aa4:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    1aa8:	8b 3d 00 00 00 00    	mov    0x0,%edi
			1aaa: R_386_32	__native_tbase
    1aae:	e8 fc ff ff ff       	call   1aaf <_FX_SyncDistributedClocks+0x9f>
			1aaf: R_386_PC32	xnarch_get_cpu_time
    1ab3:	03 47 10             	add    0x10(%edi),%eax
    1ab6:	13 57 14             	adc    0x14(%edi),%edx
    1ab9:	89 04 f5 30 5f 00 00 	mov    %eax,0x5f30(,%esi,8)
			1abc: R_386_32	.bss
    1ac0:	83 04 b5 28 5f 00 00 	addl   $0x1,0x5f28(,%esi,4)
    1ac7:	01 
			1ac3: R_386_32	.bss
    1ac8:	89 14 f5 34 5f 00 00 	mov    %edx,0x5f34(,%esi,8)
			1acb: R_386_32	.bss
    1acf:	89 d1                	mov    %edx,%ecx
    1ad1:	89 c2                	mov    %eax,%edx
    1ad3:	eb 89                	jmp    1a5e <_FX_SyncDistributedClocks+0x4e>
    1ad5:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    1ad9:	8d bc 27 00 00 00 00 	lea    0x0(%edi,%eiz,1),%edi

00001ae0 <_FX_ExchangePdos.isra.3>:
    1ae0:	55                   	push   %ebp
    1ae1:	89 e5                	mov    %esp,%ebp
    1ae3:	57                   	push   %edi
    1ae4:	56                   	push   %esi
    1ae5:	53                   	push   %ebx
    1ae6:	83 ec 10             	sub    $0x10,%esp
    1ae9:	89 45 ec             	mov    %eax,-0x14(%ebp)
    1aec:	8b 04 85 00 00 00 00 	mov    0x0(,%eax,4),%eax
			1aef: R_386_32	fx_pdo_entry_index
    1af3:	89 55 e8             	mov    %edx,-0x18(%ebp)
    1af6:	89 4d e4             	mov    %ecx,-0x1c(%ebp)
    1af9:	8b 7d 08             	mov    0x8(%ebp),%edi
    1afc:	c7 05 4c 5f 00 00 00 	movl   $0x0,0x5f4c
    1b03:	00 00 00 
			1afe: R_386_32	.bss
    1b06:	c7 45 f0 00 00 00 00 	movl   $0x0,-0x10(%ebp)
    1b0d:	85 c0                	test   %eax,%eax
    1b0f:	0f 8e 7d 00 00 00    	jle    1b92 <_FX_ExchangePdos.isra.3+0xb2>
    1b15:	8d 76 00             	lea    0x0(%esi),%esi
    1b18:	8b 17                	mov    (%edi),%edx
    1b1a:	31 f6                	xor    %esi,%esi
    1b1c:	89 fb                	mov    %edi,%ebx
    1b1e:	c7 05 48 5f 00 00 00 	movl   $0x0,0x5f48
    1b25:	00 00 00 
			1b20: R_386_32	.bss
    1b28:	81 fa fe ff 00 00    	cmp    $0xfffe,%edx
    1b2e:	75 1e                	jne    1b4e <_FX_ExchangePdos.isra.3+0x6e>
    1b30:	eb 3e                	jmp    1b70 <_FX_ExchangePdos.isra.3+0x90>
    1b32:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi
    1b38:	83 c3 0c             	add    $0xc,%ebx
    1b3b:	8b 13                	mov    (%ebx),%edx
    1b3d:	83 c6 01             	add    $0x1,%esi
    1b40:	89 35 48 5f 00 00    	mov    %esi,0x5f48
			1b42: R_386_32	.bss
    1b46:	81 fa fe ff 00 00    	cmp    $0xfffe,%edx
    1b4c:	74 22                	je     1b70 <_FX_ExchangePdos.isra.3+0x90>
    1b4e:	81 fa ff ff 00 00    	cmp    $0xffff,%edx
    1b54:	74 e2                	je     1b38 <_FX_ExchangePdos.isra.3+0x58>
    1b56:	83 7b 04 01          	cmpl   $0x1,0x4(%ebx)
    1b5a:	75 dc                	jne    1b38 <_FX_ExchangePdos.isra.3+0x58>
    1b5c:	8b 45 e8             	mov    -0x18(%ebp),%eax
    1b5f:	8b 4b 08             	mov    0x8(%ebx),%ecx
    1b62:	01 d0                	add    %edx,%eax
    1b64:	03 55 e4             	add    -0x1c(%ebp),%edx
    1b67:	e8 fc ff ff ff       	call   1b68 <_FX_ExchangePdos.isra.3+0x88>
			1b68: R_386_PC32	memcpy
    1b6c:	eb ca                	jmp    1b38 <_FX_ExchangePdos.isra.3+0x58>
    1b6e:	66 90                	xchg   %ax,%ax
    1b70:	8b 45 f0             	mov    -0x10(%ebp),%eax
    1b73:	81 c7 80 01 00 00    	add    $0x180,%edi
    1b79:	8b 4d ec             	mov    -0x14(%ebp),%ecx
    1b7c:	83 c0 01             	add    $0x1,%eax
    1b7f:	3b 04 8d 00 00 00 00 	cmp    0x0(,%ecx,4),%eax
			1b82: R_386_32	fx_pdo_entry_index
    1b86:	a3 4c 5f 00 00       	mov    %eax,0x5f4c
			1b87: R_386_32	.bss
    1b8b:	7d 05                	jge    1b92 <_FX_ExchangePdos.isra.3+0xb2>
    1b8d:	89 45 f0             	mov    %eax,-0x10(%ebp)
    1b90:	eb 86                	jmp    1b18 <_FX_ExchangePdos.isra.3+0x38>
    1b92:	83 c4 10             	add    $0x10,%esp
    1b95:	5b                   	pop    %ebx
    1b96:	5e                   	pop    %esi
    1b97:	5f                   	pop    %edi
    1b98:	5d                   	pop    %ebp
    1b99:	c3                   	ret    
    1b9a:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi

00001ba0 <__FX_RequestEnable>:
    1ba0:	55                   	push   %ebp
    1ba1:	89 e5                	mov    %esp,%ebp
    1ba3:	56                   	push   %esi
    1ba4:	89 c6                	mov    %eax,%esi
    1ba6:	53                   	push   %ebx
    1ba7:	89 d3                	mov    %edx,%ebx
    1ba9:	89 c2                	mov    %eax,%edx
    1bab:	83 ec 0c             	sub    $0xc,%esp
    1bae:	a1 00 00 00 00       	mov    0x0,%eax
			1baf: R_386_32	fx_macshm_ptr
    1bb3:	e8 fc ff ff ff       	call   1bb4 <__FX_RequestEnable+0x14>
			1bb4: R_386_PC32	FX_MacChnCmdEnable
    1bb8:	85 c0                	test   %eax,%eax
    1bba:	78 0f                	js     1bcb <__FX_RequestEnable+0x2b>
    1bbc:	83 03 01             	addl   $0x1,(%ebx)
    1bbf:	b8 01 00 00 00       	mov    $0x1,%eax
    1bc4:	83 c4 0c             	add    $0xc,%esp
    1bc7:	5b                   	pop    %ebx
    1bc8:	5e                   	pop    %esi
    1bc9:	5d                   	pop    %ebp
    1bca:	c3                   	ret    
    1bcb:	c7 03 00 00 00 00    	movl   $0x0,(%ebx)
    1bd1:	89 74 24 08          	mov    %esi,0x8(%esp)
    1bd5:	c7 44 24 04 c0 04 00 	movl   $0x4c0,0x4(%esp)
    1bdc:	00 
			1bd9: R_386_32	.rodata
    1bdd:	c7 04 24 4c 08 00 00 	movl   $0x84c,(%esp)
			1be0: R_386_32	.rodata.str1.4
    1be4:	e8 fc ff ff ff       	call   1be5 <__FX_RequestEnable+0x45>
			1be5: R_386_PC32	ERROR_LOG
    1be9:	b8 ff ff ff ff       	mov    $0xffffffff,%eax
    1bee:	eb d4                	jmp    1bc4 <__FX_RequestEnable+0x24>

00001bf0 <__FX_WaitEnable>:
    1bf0:	55                   	push   %ebp
    1bf1:	89 e5                	mov    %esp,%ebp
    1bf3:	56                   	push   %esi
    1bf4:	89 d6                	mov    %edx,%esi
    1bf6:	53                   	push   %ebx
    1bf7:	89 c2                	mov    %eax,%edx
    1bf9:	89 c3                	mov    %eax,%ebx
    1bfb:	83 ec 0c             	sub    $0xc,%esp
    1bfe:	a1 00 00 00 00       	mov    0x0,%eax
			1bff: R_386_32	fx_macshm_ptr
    1c03:	e8 fc ff ff ff       	call   1c04 <__FX_WaitEnable+0x14>
			1c04: R_386_PC32	FX_MacChnCmdEnableOK
    1c08:	85 c0                	test   %eax,%eax
    1c0a:	74 34                	je     1c40 <__FX_WaitEnable+0x50>
    1c0c:	83 f8 02             	cmp    $0x2,%eax
    1c0f:	c7 06 00 00 00 00    	movl   $0x0,(%esi)
    1c15:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1c19:	c7 44 24 04 b0 04 00 	movl   $0x4b0,0x4(%esp)
    1c20:	00 
			1c1d: R_386_32	.rodata
    1c21:	74 26                	je     1c49 <__FX_WaitEnable+0x59>
    1c23:	c7 04 24 b5 01 00 00 	movl   $0x1b5,(%esp)
			1c26: R_386_32	.rodata.str1.1
    1c2a:	e8 fc ff ff ff       	call   1c2b <__FX_WaitEnable+0x3b>
			1c2b: R_386_PC32	INFO_LOG
    1c2f:	b8 01 00 00 00       	mov    $0x1,%eax
    1c34:	83 c4 0c             	add    $0xc,%esp
    1c37:	5b                   	pop    %ebx
    1c38:	5e                   	pop    %esi
    1c39:	5d                   	pop    %ebp
    1c3a:	c3                   	ret    
    1c3b:	90                   	nop
    1c3c:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    1c40:	83 c4 0c             	add    $0xc,%esp
    1c43:	31 c0                	xor    %eax,%eax
    1c45:	5b                   	pop    %ebx
    1c46:	5e                   	pop    %esi
    1c47:	5d                   	pop    %ebp
    1c48:	c3                   	ret    
    1c49:	c7 04 24 78 08 00 00 	movl   $0x878,(%esp)
			1c4c: R_386_32	.rodata.str1.4
    1c50:	e8 fc ff ff ff       	call   1c51 <__FX_WaitEnable+0x61>
			1c51: R_386_PC32	ERROR_LOG
    1c55:	a1 00 00 00 00       	mov    0x0,%eax
			1c56: R_386_32	fx_macshm_ptr
    1c5a:	89 da                	mov    %ebx,%edx
    1c5c:	e8 fc ff ff ff       	call   1c5d <__FX_WaitEnable+0x6d>
			1c5d: R_386_PC32	FX_MacChnCmdKill
    1c61:	b8 ff ff ff ff       	mov    $0xffffffff,%eax
    1c66:	eb cc                	jmp    1c34 <__FX_WaitEnable+0x44>
    1c68:	90                   	nop
    1c69:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi

00001c70 <_FX_RunTransferToPosition>:
    1c70:	55                   	push   %ebp
    1c71:	89 e5                	mov    %esp,%ebp
    1c73:	57                   	push   %edi
    1c74:	56                   	push   %esi
    1c75:	53                   	push   %ebx
    1c76:	89 c3                	mov    %eax,%ebx
    1c78:	83 e4 f8             	and    $0xfffffff8,%esp
    1c7b:	83 ec 14             	sub    $0x14,%esp
    1c7e:	83 f8 01             	cmp    $0x1,%eax
    1c81:	8b 34 85 2c 56 00 00 	mov    0x562c(,%eax,4),%esi
			1c84: R_386_32	.bss
    1c88:	19 c9                	sbb    %ecx,%ecx
    1c8a:	f7 d1                	not    %ecx
    1c8c:	81 e1 e8 03 00 00    	and    $0x3e8,%ecx
    1c92:	89 0d 34 56 00 00    	mov    %ecx,0x5634
			1c94: R_386_32	.bss
    1c98:	83 fe 01             	cmp    $0x1,%esi
    1c9b:	0f 84 b7 01 00 00    	je     1e58 <_FX_RunTransferToPosition+0x1e8>
    1ca1:	0f 8e 91 00 00 00    	jle    1d38 <_FX_RunTransferToPosition+0xc8>
    1ca7:	83 fe 02             	cmp    $0x2,%esi
    1caa:	74 34                	je     1ce0 <_FX_RunTransferToPosition+0x70>
    1cac:	83 fe 03             	cmp    $0x3,%esi
    1caf:	90                   	nop
    1cb0:	0f 85 52 01 00 00    	jne    1e08 <_FX_RunTransferToPosition+0x198>
    1cb6:	89 d7                	mov    %edx,%edi
    1cb8:	8d 14 85 2c 56 00 00 	lea    0x562c(,%eax,4),%edx
			1cbb: R_386_32	.bss
    1cbf:	e8 2c ff ff ff       	call   1bf0 <__FX_WaitEnable>
    1cc4:	85 c0                	test   %eax,%eax
    1cc6:	0f 88 54 02 00 00    	js     1f20 <_FX_RunTransferToPosition+0x2b0>
    1ccc:	83 f8 01             	cmp    $0x1,%eax
    1ccf:	0f 84 0b 02 00 00    	je     1ee0 <_FX_RunTransferToPosition+0x270>
    1cd5:	8d 65 f4             	lea    -0xc(%ebp),%esp
    1cd8:	5b                   	pop    %ebx
    1cd9:	5e                   	pop    %esi
    1cda:	5f                   	pop    %edi
    1cdb:	5d                   	pop    %ebp
    1cdc:	c3                   	ret    
    1cdd:	8d 76 00             	lea    0x0(%esi),%esi
    1ce0:	8d 14 85 2c 56 00 00 	lea    0x562c(,%eax,4),%edx
			1ce3: R_386_32	.bss
    1ce7:	e8 b4 fe ff ff       	call   1ba0 <__FX_RequestEnable>
    1cec:	85 c0                	test   %eax,%eax
    1cee:	79 e5                	jns    1cd5 <_FX_RunTransferToPosition+0x65>
    1cf0:	a1 34 56 00 00       	mov    0x5634,%eax
			1cf1: R_386_32	.bss
    1cf5:	8b 15 00 00 00 00    	mov    0x0,%edx
			1cf7: R_386_32	fx_sysinfo_ptr
    1cfb:	c7 84 82 a8 0f 00 00 	movl   $0x8,0xfa8(%edx,%eax,4)
    1d02:	08 00 00 00 
    1d06:	8b 15 00 00 00 00    	mov    0x0,%edx
			1d08: R_386_32	fx_sysinfo_ptr
    1d0c:	c7 84 82 a4 0f 00 00 	movl   $0x64,0xfa4(%edx,%eax,4)
    1d13:	64 00 00 00 
    1d17:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1d1b:	c7 44 24 04 20 05 00 	movl   $0x520,0x4(%esp)
    1d22:	00 
			1d1f: R_386_32	.rodata
    1d23:	c7 04 24 48 09 00 00 	movl   $0x948,(%esp)
			1d26: R_386_32	.rodata.str1.4
    1d2a:	e8 fc ff ff ff       	call   1d2b <_FX_RunTransferToPosition+0xbb>
			1d2b: R_386_PC32	ERROR_LOG
    1d2f:	eb a4                	jmp    1cd5 <_FX_RunTransferToPosition+0x65>
    1d31:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    1d38:	85 f6                	test   %esi,%esi
    1d3a:	0f 85 c8 00 00 00    	jne    1e08 <_FX_RunTransferToPosition+0x198>
    1d40:	e8 fc ff ff ff       	call   1d41 <_FX_RunTransferToPosition+0xd1>
			1d41: R_386_PC32	FX_Psi2SiEmpty
    1d45:	a1 00 00 00 00       	mov    0x0,%eax
			1d46: R_386_32	fx_macshm_ptr
    1d4a:	89 da                	mov    %ebx,%edx
    1d4c:	c7 44 24 10 00 00 00 	movl   $0x0,0x10(%esp)
    1d53:	00 
    1d54:	e8 fc ff ff ff       	call   1d55 <_FX_RunTransferToPosition+0xe5>
			1d55: R_386_PC32	FX_MacGetChnCtrlState
    1d59:	85 c0                	test   %eax,%eax
    1d5b:	89 c6                	mov    %eax,%esi
    1d5d:	0f 84 d8 01 00 00    	je     1f3b <_FX_RunTransferToPosition+0x2cb>
    1d63:	a1 00 00 00 00       	mov    0x0,%eax
			1d64: R_386_32	fx_cfgshm_ptr
    1d68:	69 fb 0c 0c 00 00    	imul   $0xc0c,%ebx,%edi
    1d6e:	8d 4c 24 10          	lea    0x10(%esp),%ecx
    1d72:	89 da                	mov    %ebx,%edx
    1d74:	01 f8                	add    %edi,%eax
    1d76:	dd 80 c0 00 00 00    	fldl   0xc0(%eax)
    1d7c:	a1 00 00 00 00       	mov    0x0,%eax
			1d7d: R_386_32	fx_macshm_ptr
    1d81:	dd 1c 24             	fstpl  (%esp)
    1d84:	e8 fc ff ff ff       	call   1d85 <_FX_RunTransferToPosition+0x115>
			1d85: R_386_PC32	FX_MacCheckEncoderDiff
    1d89:	84 c0                	test   %al,%al
    1d8b:	0f 84 2c 02 00 00    	je     1fbd <_FX_RunTransferToPosition+0x34d>
    1d91:	83 fe 05             	cmp    $0x5,%esi
    1d94:	0f 84 ee 00 00 00    	je     1e88 <_FX_RunTransferToPosition+0x218>
    1d9a:	83 fe 02             	cmp    $0x2,%esi
    1d9d:	8d 76 00             	lea    0x0(%esi),%esi
    1da0:	0f 84 da 01 00 00    	je     1f80 <_FX_RunTransferToPosition+0x310>
    1da6:	83 fe 01             	cmp    $0x1,%esi
    1da9:	0f 84 f1 01 00 00    	je     1fa0 <_FX_RunTransferToPosition+0x330>
    1daf:	69 cb c4 00 00 00    	imul   $0xc4,%ebx,%ecx
    1db5:	a1 00 00 00 00       	mov    0x0,%eax
			1db6: R_386_32	fx_macshm_ptr
    1dba:	89 da                	mov    %ebx,%edx
    1dbc:	81 c1 00 cd 00 00    	add    $0xcd00,%ecx
			1dbe: R_386_32	.bss
    1dc2:	e8 fc ff ff ff       	call   1dc3 <_FX_RunTransferToPosition+0x153>
			1dc3: R_386_PC32	FX_MacSyncChnPosition
    1dc7:	a1 00 00 00 00       	mov    0x0,%eax
			1dc8: R_386_32	fx_macshm_ptr
    1dcc:	89 da                	mov    %ebx,%edx
    1dce:	e8 fc ff ff ff       	call   1dcf <_FX_RunTransferToPosition+0x15f>
			1dcf: R_386_PC32	FX_MacChnCmdChangeToPositonMode
    1dd3:	85 c0                	test   %eax,%eax
    1dd5:	0f 88 12 02 00 00    	js     1fed <_FX_RunTransferToPosition+0x37d>
    1ddb:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1ddf:	83 04 9d 2c 56 00 00 	addl   $0x1,0x562c(,%ebx,4)
    1de6:	01 
			1de2: R_386_32	.bss
    1de7:	c7 44 24 04 00 05 00 	movl   $0x500,0x4(%esp)
    1dee:	00 
			1deb: R_386_32	.rodata
    1def:	c7 04 24 dc 08 00 00 	movl   $0x8dc,(%esp)
			1df2: R_386_32	.rodata.str1.4
    1df6:	e8 fc ff ff ff       	call   1df7 <_FX_RunTransferToPosition+0x187>
			1df7: R_386_PC32	INFO_LOG
    1dfb:	8d 65 f4             	lea    -0xc(%ebp),%esp
    1dfe:	5b                   	pop    %ebx
    1dff:	5e                   	pop    %esi
    1e00:	5f                   	pop    %edi
    1e01:	5d                   	pop    %ebp
    1e02:	c3                   	ret    
    1e03:	90                   	nop
    1e04:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    1e08:	a1 00 00 00 00       	mov    0x0,%eax
			1e09: R_386_32	fx_sysinfo_ptr
    1e0d:	c7 84 88 a8 0f 00 00 	movl   $0xc,0xfa8(%eax,%ecx,4)
    1e14:	0c 00 00 00 
    1e18:	a1 00 00 00 00       	mov    0x0,%eax
			1e19: R_386_32	fx_sysinfo_ptr
    1e1d:	c7 84 88 a4 0f 00 00 	movl   $0x64,0xfa4(%eax,%ecx,4)
    1e24:	64 00 00 00 
    1e28:	8b 04 9d 2c 56 00 00 	mov    0x562c(,%ebx,4),%eax
			1e2b: R_386_32	.bss
    1e2f:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1e33:	c7 44 24 04 20 05 00 	movl   $0x520,0x4(%esp)
    1e3a:	00 
			1e37: R_386_32	.rodata
    1e3b:	c7 04 24 70 09 00 00 	movl   $0x970,(%esp)
			1e3e: R_386_32	.rodata.str1.4
    1e42:	89 44 24 0c          	mov    %eax,0xc(%esp)
    1e46:	e8 fc ff ff ff       	call   1e47 <_FX_RunTransferToPosition+0x1d7>
			1e47: R_386_PC32	ERROR_LOG
    1e4b:	8d 65 f4             	lea    -0xc(%ebp),%esp
    1e4e:	5b                   	pop    %ebx
    1e4f:	5e                   	pop    %esi
    1e50:	5f                   	pop    %edi
    1e51:	5d                   	pop    %ebp
    1e52:	c3                   	ret    
    1e53:	90                   	nop
    1e54:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    1e58:	89 c2                	mov    %eax,%edx
    1e5a:	a1 00 00 00 00       	mov    0x0,%eax
			1e5b: R_386_32	fx_macshm_ptr
    1e5f:	e8 fc ff ff ff       	call   1e60 <_FX_RunTransferToPosition+0x1f0>
			1e60: R_386_PC32	FX_MacChnCmdChangeToPositonModeOK
    1e64:	85 c0                	test   %eax,%eax
    1e66:	0f 84 69 fe ff ff    	je     1cd5 <_FX_RunTransferToPosition+0x65>
    1e6c:	83 f8 02             	cmp    $0x2,%eax
    1e6f:	0f 84 a0 01 00 00    	je     2015 <_FX_RunTransferToPosition+0x3a5>
    1e75:	83 04 9d 2c 56 00 00 	addl   $0x1,0x562c(,%ebx,4)
    1e7c:	01 
			1e78: R_386_32	.bss
    1e7d:	8d 65 f4             	lea    -0xc(%ebp),%esp
    1e80:	5b                   	pop    %ebx
    1e81:	5e                   	pop    %esi
    1e82:	5f                   	pop    %edi
    1e83:	5d                   	pop    %ebp
    1e84:	c3                   	ret    
    1e85:	8d 76 00             	lea    0x0(%esi),%esi
    1e88:	03 3d 00 00 00 00    	add    0x0,%edi
			1e8a: R_386_32	fx_cfgshm_ptr
    1e8e:	8b 87 bc 00 00 00    	mov    0xbc(%edi),%eax
    1e94:	85 c0                	test   %eax,%eax
    1e96:	0f 85 13 ff ff ff    	jne    1daf <_FX_RunTransferToPosition+0x13f>
    1e9c:	69 cb c4 00 00 00    	imul   $0xc4,%ebx,%ecx
    1ea2:	a1 00 00 00 00       	mov    0x0,%eax
			1ea3: R_386_32	fx_macshm_ptr
    1ea7:	89 da                	mov    %ebx,%edx
    1ea9:	83 04 9d 2c 56 00 00 	addl   $0x2,0x562c(,%ebx,4)
    1eb0:	02 
			1eac: R_386_32	.bss
    1eb1:	81 c1 00 cd 00 00    	add    $0xcd00,%ecx
			1eb3: R_386_32	.bss
    1eb7:	e8 fc ff ff ff       	call   1eb8 <_FX_RunTransferToPosition+0x248>
			1eb8: R_386_PC32	FX_MacSyncChnPosition
    1ebc:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1ec0:	c7 44 24 04 00 05 00 	movl   $0x500,0x4(%esp)
    1ec7:	00 
			1ec4: R_386_32	.rodata
    1ec8:	c7 04 24 d4 05 00 00 	movl   $0x5d4,(%esp)
			1ecb: R_386_32	.rodata.str1.4
    1ecf:	e8 fc ff ff ff       	call   1ed0 <_FX_RunTransferToPosition+0x260>
			1ed0: R_386_PC32	INFO_LOG
    1ed4:	e9 fc fd ff ff       	jmp    1cd5 <_FX_RunTransferToPosition+0x65>
    1ed9:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    1ee0:	8b 15 34 56 00 00    	mov    0x5634,%edx
			1ee2: R_386_32	.bss
    1ee6:	a1 00 00 00 00       	mov    0x0,%eax
			1ee7: R_386_32	fx_sysinfo_ptr
    1eeb:	89 bc 90 a4 0f 00 00 	mov    %edi,0xfa4(%eax,%edx,4)
    1ef2:	89 f8                	mov    %edi,%eax
    1ef4:	e8 07 e1 ff ff       	call   0 <_FX_GetArmStateStr>
    1ef9:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1efd:	89 44 24 0c          	mov    %eax,0xc(%esp)
    1f01:	c7 44 24 04 20 05 00 	movl   $0x520,0x4(%esp)
    1f08:	00 
			1f05: R_386_32	.rodata
    1f09:	c7 04 24 d3 01 00 00 	movl   $0x1d3,(%esp)
			1f0c: R_386_32	.rodata.str1.1
    1f10:	e8 fc ff ff ff       	call   1f11 <_FX_RunTransferToPosition+0x2a1>
			1f11: R_386_PC32	INFO_LOG
    1f15:	e9 bb fd ff ff       	jmp    1cd5 <_FX_RunTransferToPosition+0x65>
    1f1a:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi
    1f20:	a1 34 56 00 00       	mov    0x5634,%eax
			1f21: R_386_32	.bss
    1f25:	8b 15 00 00 00 00    	mov    0x0,%edx
			1f27: R_386_32	fx_sysinfo_ptr
    1f2b:	c7 84 82 a8 0f 00 00 	movl   $0x9,0xfa8(%edx,%eax,4)
    1f32:	09 00 00 00 
    1f36:	e9 cb fd ff ff       	jmp    1d06 <_FX_RunTransferToPosition+0x96>
    1f3b:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1f3f:	c7 44 24 04 00 05 00 	movl   $0x500,0x4(%esp)
    1f46:	00 
			1f43: R_386_32	.rodata
    1f47:	c7 04 24 34 05 00 00 	movl   $0x534,(%esp)
			1f4a: R_386_32	.rodata.str1.4
    1f4e:	c7 04 9d 2c 56 00 00 	movl   $0x0,0x562c(,%ebx,4)
    1f55:	00 00 00 00 
			1f51: R_386_32	.bss
    1f59:	e8 fc ff ff ff       	call   1f5a <_FX_RunTransferToPosition+0x2ea>
			1f5a: R_386_PC32	ERROR_LOG
    1f5e:	a1 34 56 00 00       	mov    0x5634,%eax
			1f5f: R_386_32	.bss
    1f63:	8b 15 00 00 00 00    	mov    0x0,%edx
			1f65: R_386_32	fx_sysinfo_ptr
    1f69:	c7 84 82 a8 0f 00 00 	movl   $0x4,0xfa8(%edx,%eax,4)
    1f70:	04 00 00 00 
    1f74:	e9 8d fd ff ff       	jmp    1d06 <_FX_RunTransferToPosition+0x96>
    1f79:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    1f80:	03 3d 00 00 00 00    	add    0x0,%edi
			1f82: R_386_32	fx_cfgshm_ptr
    1f86:	83 bf bc 00 00 00 01 	cmpl   $0x1,0xbc(%edi)
    1f8d:	0f 85 1c fe ff ff    	jne    1daf <_FX_RunTransferToPosition+0x13f>
    1f93:	e9 04 ff ff ff       	jmp    1e9c <_FX_RunTransferToPosition+0x22c>
    1f98:	90                   	nop
    1f99:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    1fa0:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1fa4:	c7 44 24 04 00 05 00 	movl   $0x500,0x4(%esp)
    1fab:	00 
			1fa8: R_386_32	.rodata
    1fac:	c7 04 24 04 06 00 00 	movl   $0x604,(%esp)
			1faf: R_386_32	.rodata.str1.4
    1fb3:	e8 fc ff ff ff       	call   1fb4 <_FX_RunTransferToPosition+0x344>
			1fb4: R_386_PC32	WARN_LOG
    1fb8:	e9 f2 fd ff ff       	jmp    1daf <_FX_RunTransferToPosition+0x13f>
    1fbd:	8b 44 24 10          	mov    0x10(%esp),%eax
    1fc1:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1fc5:	c7 44 24 04 00 05 00 	movl   $0x500,0x4(%esp)
    1fcc:	00 
			1fc9: R_386_32	.rodata
    1fcd:	c7 04 24 64 05 00 00 	movl   $0x564,(%esp)
			1fd0: R_386_32	.rodata.str1.4
    1fd4:	c7 04 9d 2c 56 00 00 	movl   $0x0,0x562c(,%ebx,4)
    1fdb:	00 00 00 00 
			1fd7: R_386_32	.bss
    1fdf:	89 44 24 0c          	mov    %eax,0xc(%esp)
    1fe3:	e8 fc ff ff ff       	call   1fe4 <_FX_RunTransferToPosition+0x374>
			1fe4: R_386_PC32	ERROR_LOG
    1fe8:	e9 71 ff ff ff       	jmp    1f5e <_FX_RunTransferToPosition+0x2ee>
    1fed:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    1ff1:	c7 44 24 04 00 05 00 	movl   $0x500,0x4(%esp)
    1ff8:	00 
			1ff5: R_386_32	.rodata
    1ff9:	c7 04 24 a0 08 00 00 	movl   $0x8a0,(%esp)
			1ffc: R_386_32	.rodata.str1.4
    2000:	c7 04 9d 2c 56 00 00 	movl   $0x0,0x562c(,%ebx,4)
    2007:	00 00 00 00 
			2003: R_386_32	.bss
    200b:	e8 fc ff ff ff       	call   200c <_FX_RunTransferToPosition+0x39c>
			200c: R_386_PC32	ERROR_LOG
    2010:	e9 49 ff ff ff       	jmp    1f5e <_FX_RunTransferToPosition+0x2ee>
    2015:	89 5c 24 08          	mov    %ebx,0x8(%esp)
    2019:	c7 44 24 04 d3 04 00 	movl   $0x4d3,0x4(%esp)
    2020:	00 
			201d: R_386_32	.rodata
    2021:	c7 04 24 10 09 00 00 	movl   $0x910,(%esp)
			2024: R_386_32	.rodata.str1.4
    2028:	c7 04 9d 2c 56 00 00 	movl   $0x0,0x562c(,%ebx,4)
    202f:	00 00 00 00 
			202b: R_386_32	.bss
    2033:	e8 fc ff ff ff       	call   2034 <_FX_RunTransferToPosition+0x3c4>
			2034: R_386_PC32	ERROR_LOG
    2038:	a1 34 56 00 00       	mov    0x5634,%eax
			2039: R_386_32	.bss
    203d:	8b 15 00 00 00 00    	mov    0x0,%edx
			203f: R_386_32	fx_sysinfo_ptr
    2043:	c7 84 82 a8 0f 00 00 	movl   $0x5,0xfa8(%edx,%eax,4)
    204a:	05 00 00 00 
    204e:	e9 b3 fc ff ff       	jmp    1d06 <_FX_RunTransferToPosition+0x96>
    2053:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi
    2059:	8d bc 27 00 00 00 00 	lea    0x0(%edi,%eiz,1),%edi

00002060 <__FX_WaitTransToSensorMode>:
    2060:	55                   	push   %ebp
    2061:	89 e5                	mov    %esp,%ebp
    2063:	56                   	push   %esi
    2064:	89 c6                	mov    %eax,%esi
    2066:	53                   	push   %ebx
    2067:	89 d3                	mov    %edx,%ebx
    2069:	89 c2                	mov    %eax,%edx
    206b:	83 ec 0c             	sub    $0xc,%esp
    206e:	a1 00 00 00 00       	mov    0x0,%eax
			206f: R_386_32	fx_macshm_ptr
    2073:	e8 fc ff ff ff       	call   2074 <__FX_WaitTransToSensorMode+0x14>
			2074: R_386_PC32	FX_MacChnCmdChangeToDragModeWithSensorOK
    2078:	85 c0                	test   %eax,%eax
    207a:	74 14                	je     2090 <__FX_WaitTransToSensorMode+0x30>
    207c:	83 f8 02             	cmp    $0x2,%eax
    207f:	74 18                	je     2099 <__FX_WaitTransToSensorMode+0x39>
    2081:	83 03 01             	addl   $0x1,(%ebx)
    2084:	b8 01 00 00 00       	mov    $0x1,%eax
    2089:	83 c4 0c             	add    $0xc,%esp
    208c:	5b                   	pop    %ebx
    208d:	5e                   	pop    %esi
    208e:	5d                   	pop    %ebp
    208f:	c3                   	ret    
    2090:	83 c4 0c             	add    $0xc,%esp
    2093:	31 c0                	xor    %eax,%eax
    2095:	5b                   	pop    %ebx
    2096:	5e                   	pop    %esi
    2097:	5d                   	pop    %ebp
    2098:	c3                   	ret    
    2099:	c7 03 00 00 00 00    	movl   $0x0,(%ebx)
    209f:	89 74 24 08          	mov    %esi,0x8(%esp)
    20a3:	c7 44 24 04 4b 04 00 	movl   $0x44b,0x4(%esp)
    20aa:	00 
			20a7: R_386_32	.rodata
    20ab:	c7 04 24 ac 09 00 00 	movl   $0x9ac,(%esp)
			20ae: R_386_32	.rodata.str1.4
    20b2:	e8 fc ff ff ff       	call   20b3 <__FX_WaitTransToSensorMode+0x53>
			20b3: R_386_PC32	ERROR_LOG
    20b7:	b8 ff ff ff ff       	mov    $0xffffffff,%eax
    20bc:	eb cb                	jmp    2089 <__FX_WaitTransToSensorMode+0x29>
    20be:	66 90                	xchg   %ax,%ax

000020c0 <_FX_RunLoop>:
    20c0:	55                   	push   %ebp
    20c1:	89 e5                	mov    %esp,%ebp
    20c3:	57                   	push   %edi
    20c4:	56                   	push   %esi
    20c5:	89 c6                	mov    %eax,%esi
    20c7:	53                   	push   %ebx
    20c8:	83 e4 f8             	and    $0xfffffff8,%esp
    20cb:	81 ec 90 00 00 00    	sub    $0x90,%esp
    20d1:	83 f8 01             	cmp    $0x1,%eax
    20d4:	19 d2                	sbb    %edx,%edx
    20d6:	81 e2 18 fc ff ff    	and    $0xfffffc18,%edx
    20dc:	83 f8 01             	cmp    $0x1,%eax
    20df:	19 c0                	sbb    %eax,%eax
    20e1:	f7 d0                	not    %eax
    20e3:	25 e8 03 00 00       	and    $0x3e8,%eax
    20e8:	80 3d e0 60 00 00 00 	cmpb   $0x0,0x60e0
			20ea: R_386_32	.bss
    20ef:	a3 f8 5e 00 00       	mov    %eax,0x5ef8
			20f0: R_386_32	.bss
    20f4:	a1 00 00 00 00       	mov    0x0,%eax
			20f5: R_386_32	fx_sysinfo_ptr
    20f9:	8b 84 90 44 1f 00 00 	mov    0x1f44(%eax,%edx,4),%eax
    2100:	a3 f4 5e 00 00       	mov    %eax,0x5ef4
			2101: R_386_32	.bss
    2105:	0f 84 45 01 00 00    	je     2250 <_FX_RunLoop+0x190>
    210b:	8d 50 fd             	lea    -0x3(%eax),%edx
    210e:	83 fa 01             	cmp    $0x1,%edx
    2111:	76 05                	jbe    2118 <_FX_RunLoop+0x58>
    2113:	83 f8 67             	cmp    $0x67,%eax
    2116:	75 18                	jne    2130 <_FX_RunLoop+0x70>
    2118:	83 f8 6d             	cmp    $0x6d,%eax
    211b:	0f 87 1f 01 00 00    	ja     2240 <_FX_RunLoop+0x180>
    2121:	ff 24 85 b8 01 00 00 	jmp    *0x1b8(,%eax,4)
			2124: R_386_32	.rodata
    2128:	90                   	nop
    2129:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    2130:	83 f8 68             	cmp    $0x68,%eax
    2133:	0f 84 57 01 00 00    	je     2290 <_FX_RunLoop+0x1d0>
    2139:	83 f8 65             	cmp    $0x65,%eax
    213c:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    2140:	0f 84 32 01 00 00    	je     2278 <_FX_RunLoop+0x1b8>
    2146:	31 c0                	xor    %eax,%eax
    2148:	b9 10 00 00 00       	mov    $0x10,%ecx
    214d:	8b 1d 00 00 00 00    	mov    0x0,%ebx
			214f: R_386_32	fx_sysinfo_ptr
    2153:	8d 7c 24 50          	lea    0x50(%esp),%edi
    2157:	f3 ab                	rep stos %eax,%es:(%edi)
    2159:	a1 f8 5e 00 00       	mov    0x5ef8,%eax
			215a: R_386_32	.bss
    215e:	8d 7c 24 50          	lea    0x50(%esp),%edi
    2162:	89 7c 24 04          	mov    %edi,0x4(%esp)
    2166:	8d 8c c3 c0 9c 00 00 	lea    0x9cc0(%ebx,%eax,8),%ecx
    216d:	8d 94 c3 40 9c 00 00 	lea    0x9c40(%ebx,%eax,8),%edx
    2174:	8d 9c c3 30 a0 00 00 	lea    0xa030(%ebx,%eax,8),%ebx
    217b:	89 f0                	mov    %esi,%eax
    217d:	89 1c 24             	mov    %ebx,(%esp)
    2180:	0f b6 c0             	movzbl %al,%eax
    2183:	e8 fc ff ff ff       	call   2184 <_FX_RunLoop+0xc4>
			2184: R_386_PC32	FX_DCTRL_JImp_CalGrav
    2188:	a1 f4 5e 00 00       	mov    0x5ef4,%eax
			2189: R_386_32	.bss
    218d:	eb 89                	jmp    2118 <_FX_RunLoop+0x58>
    218f:	90                   	nop
    2190:	8b 14 b5 20 56 00 00 	mov    0x5620(,%esi,4),%edx
			2193: R_386_32	.bss
    2197:	83 fe 01             	cmp    $0x1,%esi
    219a:	19 c0                	sbb    %eax,%eax
    219c:	f7 d0                	not    %eax
    219e:	25 e8 03 00 00       	and    $0x3e8,%eax
    21a3:	83 fa 01             	cmp    $0x1,%edx
    21a6:	a3 28 56 00 00       	mov    %eax,0x5628
			21a7: R_386_32	.bss
    21ab:	0f 84 ef 0f 00 00    	je     31a0 <_FX_RunLoop+0x10e0>
    21b1:	0f 8f a9 05 00 00    	jg     2760 <_FX_RunLoop+0x6a0>
    21b7:	85 d2                	test   %edx,%edx
    21b9:	0f 85 59 0f 00 00    	jne    3118 <_FX_RunLoop+0x1058>
    21bf:	89 f0                	mov    %esi,%eax
    21c1:	e8 fc ff ff ff       	call   21c2 <_FX_RunLoop+0x102>
			21c2: R_386_PC32	FX_Psi2SiEmpty
    21c6:	8b 15 00 00 00 00    	mov    0x0,%edx
			21c8: R_386_32	fx_sysinfo_ptr
    21cc:	a1 28 56 00 00       	mov    0x5628,%eax
			21cd: R_386_32	.bss
    21d1:	83 bc 82 c0 12 00 00 	cmpl   $0x1,0x12c0(%edx,%eax,4)
    21d8:	01 
    21d9:	75 0f                	jne    21ea <_FX_RunLoop+0x12a>
    21db:	8b 9c 82 10 13 00 00 	mov    0x1310(%edx,%eax,4),%ebx
    21e2:	85 db                	test   %ebx,%ebx
    21e4:	0f 84 ea 15 00 00    	je     37d4 <_FX_RunLoop+0x1714>
    21ea:	8d 14 b5 20 56 00 00 	lea    0x5620(,%esi,4),%edx
			21ed: R_386_32	.bss
    21f1:	b9 01 00 00 00       	mov    $0x1,%ecx
    21f6:	89 f0                	mov    %esi,%eax
    21f8:	e8 73 f1 ff ff       	call   1370 <__FX_RequestTransToSensorMode>
    21fd:	85 c0                	test   %eax,%eax
    21ff:	79 3f                	jns    2240 <_FX_RunLoop+0x180>
    2201:	a1 28 56 00 00       	mov    0x5628,%eax
			2202: R_386_32	.bss
    2206:	8b 15 00 00 00 00    	mov    0x0,%edx
			2208: R_386_32	fx_sysinfo_ptr
    220c:	c7 84 82 a8 0f 00 00 	movl   $0x6,0xfa8(%edx,%eax,4)
    2213:	06 00 00 00 
    2217:	8b 15 00 00 00 00    	mov    0x0,%edx
			2219: R_386_32	fx_sysinfo_ptr
    221d:	c7 84 82 a4 0f 00 00 	movl   $0x64,0xfa4(%edx,%eax,4)
    2224:	64 00 00 00 
    2228:	89 74 24 08          	mov    %esi,0x8(%esp)
    222c:	c7 44 24 04 98 04 00 	movl   $0x498,0x4(%esp)
    2233:	00 
			2230: R_386_32	.rodata
    2234:	c7 04 24 48 09 00 00 	movl   $0x948,(%esp)
			2237: R_386_32	.rodata.str1.4
    223b:	e8 fc ff ff ff       	call   223c <_FX_RunLoop+0x17c>
			223c: R_386_PC32	ERROR_LOG
    2240:	8d 65 f4             	lea    -0xc(%ebp),%esp
    2243:	5b                   	pop    %ebx
    2244:	5e                   	pop    %esi
    2245:	5f                   	pop    %edi
    2246:	5d                   	pop    %ebp
    2247:	c3                   	ret    
    2248:	90                   	nop
    2249:	8d b4 26 00 00 00 00 	lea    0x0(%esi,%eiz,1),%esi
    2250:	31 c0                	xor    %eax,%eax
    2252:	e8 fc ff ff ff       	call   2253 <_FX_RunLoop+0x193>
			2253: R_386_PC32	FX_DCTRL_INIT_JImp_ARM
    2257:	b8 01 00 00 00       	mov    $0x1,%eax
    225c:	e8 fc ff ff ff       	call   225d <_FX_RunLoop+0x19d>
			225d: R_386_PC32	FX_DCTRL_INIT_JImp_ARM
    2261:	c6 05 e0 60 00 00 01 	movb   $0x1,0x60e0
			2263: R_386_32	.bss
    2268:	a1 f4 5e 00 00       	mov    0x5ef4,%eax
			2269: R_386_32	.bss
    226d:	e9 99 fe ff ff       	jmp    210b <_FX_RunLoop+0x4b>
    2272:	8d b6 00 00 00 00    	lea    0x0(%esi),%esi
    2278:	ba 01 00 00 00       	mov    $0x1,%edx
    227d:	89 f0                	mov    %esi,%eax
    227f:	e8 ec f9 ff ff       	call   1c70 <_FX_RunTransferToPosition>
    2284:	eb ba                	jmp    2240 <_FX_RunLoop+0x180>
    2286:	8d 76 00             	lea    0x0(%esi),%esi
    2289:	8d bc 27 00 00 00 00 	lea    0x0(%edi,%eiz,1),%edi
    2290:	8b 14 b5 dc 02 00 00 	mov    0x2dc(,%esi,4),%edx
			2293: R_386_32	.bss
    2297:	83 fe 01             	cmp    $0x1,%esi
    229a:	19 c0                	sbb    %eax,%eax
    229c:	f7 d0                	not    %eax
    229e:	25 e8 03 00 00       	and    $0x3e8,%eax
    22a3:	83 fa 01             	cmp    $0x1,%edx
    22a6:	a3 e4 02 00 00       	mov    %eax,0x2e4
			22a7: R_386_32	.bss
    22ab:	0f 84 2f 0e 00 00    	je     30e0 <_FX_RunLoop+0x1020>
    22b1:	0f 8e c9 07 00 00    	jle    2a80 <_FX_RunLoop+0x9c0>
    22b7:	83 fa 02             	cmp    $0x2,%edx
    22ba:	0f 84 68 0f 00 00    	je     3228 <_FX_RunLoop+0x1168>
    22c0:	83 fa 03             	cmp    $0x3,%edx
    22c3:	0f 85 0f 0f 00 00    	jne    31d8 <_FX_RunLoop+0x1118>
    22c9:	8d 14 b5 dc 02 00 00 	lea    0x2dc(,%esi,4),%edx
			22cc: R_386_32	.bss
    22d0:	89 f0                	mov    %esi,%eax
    22d2:	e8 19 f9 ff ff       	call   1bf0 <__FX_WaitEnable>
    22d7:	85 c0                	test   %eax,%eax
    22d9:	0f 88 2e 15 00 00    	js     380d <_FX_RunLoop+0x174d>
    22df:	83 f8 01             	cmp    $0x1,%eax
    22e2:	0f 85 58 ff ff ff    	jne    2240 <_FX_RunLoop+0x180>
    22e8:	8b 15 e4 02 00 00    	mov    0x2e4,%edx
			22ea: R_386_32	.bss
    22ee:	a1 00 00 00 00       	mov    0x0,%eax
			22ef: R_386_32	fx_sysinfo_ptr
    22f3:	c7 84 90 a4 0f 00 00 	movl   $0x4,0xfa4(%eax,%edx,4)
    22fa:	04 00 00 00 
    22fe:	89 74 24 08          	mov    %esi,0x8(%esp)
    2302:	c7 44 24 04 ad 03 00 	movl   $0x3ad,0x4(%esp)
    2309:	00 
			2306: R_386_32	.rodata
    230a:	c7 04 24 38 0e 00 00 	movl   $0xe38,(%esp)
			230d: R_386_32	.rodata.str1.4
    2311:	e8 fc ff ff ff       	call   2312 <_FX_RunLoop+0x252>
			2312: R_386_PC32	INFO_LOG
    2316:	e9 25 ff ff ff       	jmp    2240 <_FX_RunLoop+0x180>
    231b:	90                   	nop
    231c:	8d 74 26 00          	lea    0x0(%esi,%eiz,1),%esi
    2320:	89 f0                	mov    %esi,%eax
    2322:	e8 b9 e0 ff ff       	call   3e0 <_FX_RunPosition>
    2327:	8d 65 f4             	lea    -0xc(%ebp),%esp
    232a:	5b                   	pop    %ebx
    232b:	5e                   	pop    %esi
    232c:	5f                   	pop    %edi
    232d:	5d                   	pop    %ebp
    232e:	c3                   	ret    
    232f:	90                   	nop
    2330:	8b 04 b5 00 00 00 00 	mov    0x0(,%esi,4),%eax
			2333: R_386_32	.data
    2337:	83 f8 04             	cmp    $0x4,%eax
    233a:	0f 84 f8 0b 00 00    	je     2f38 <_FX_RunLoop+0xe78>
    2340:	83 f8 05             	cmp    $0x5,%eax
    2343:	0f 85 f7 fe ff ff    	jne    2240 <_FX_RunLoop+0x180>
    2349:	a1 00 00 00 00       	mov    0x0,%eax
			234a: R_386_32	fx_macshm_ptr
    234e:	89 f2                	mov    %esi,%edx
