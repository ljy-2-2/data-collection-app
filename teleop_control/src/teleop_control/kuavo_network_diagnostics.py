"""Read local routes only; never configure networks or probe robot services."""
import ipaddress
import json
import subprocess


def inspect_route(host, namespace='current'):
    result = dict(host=host, namespace=namespace, interface='', gateway='', source='', blocked=False, detail='')
    if namespace != 'current':
        result['detail']='使用原项目 kuavons 网络；本电脑需已配置该网络'
        return result
    try:
        ipaddress.IPv4Address(host)
        query = subprocess.run(['ip','-j','route','get',host],capture_output=True,text=True,timeout=3)
        if query.returncode:
            result['detail']='无法读取本电脑路由；连接失败时请检查网卡和路由'
            return result
        routes = json.loads(query.stdout)
        route = routes[0] if routes else {}
        result.update(interface=route.get('dev',''),gateway=route.get('gateway',''),source=route.get('prefsrc',''))
        # Do not treat every VPN as wrong: only diagnose the known transparent
        # proxy interface. Never fall back to another address/namespace or TLS.
        if any(name in result['interface'].lower() for name in ('mihomo','clash')):
            result.update(blocked=True,detail=f'机器人地址 {host} 当前经代理虚拟网卡 {result["interface"]}，未走机器人直连网络。'
                '请连接机器人网线并配置可达该地址的本机网卡；在代理中排除机器人网段或关闭 TUN 接管后再检查。')
        else:
            result['detail']=f'本机网络路径：{host} → {result["interface"] or "未知网卡"}'
        return result
    except (OSError, ValueError, TypeError, IndexError, subprocess.TimeoutExpired):
        result['detail']='本机路由信息不可用'
        return result


def tls_failure(error, config, namespace, route=None):
    path = (route or {}).get('interface') or namespace
    return (f'乐聚服务 TLS 连接被对端提前关闭（{config["network_host"]}:{config["network_port"]}，网络：{path}）。'
            '尚未确认连接到机器人服务；请核对网线、网卡地址、代理路由及服务端口。'
            '未跳过证书校验，未启动遥操。原始错误：'+str(error))
