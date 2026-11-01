# HomeProxy 与校园三路集成

两个独立路径：

1. 显式直连域名：dnsmasq 将 DNS 回答加入 fw4 的 `lotus_direct4/6` 集合；
   HomeProxy 在送入 TUN 前放行，再由原有 LAN 三路策略分流，保留 HNAT。
2. sing-box 代理和残余 direct-out 连接：生成器增加专用 socket mark 0x4200，
   独立 nft output route 链按健康线路分配，连接标记 0x4301–0x4303 固定出口，
   路由标记复用原三路规则 0x4101–0x4103，并显式做路由器本机连接的源 NAT。
   关闭 sing-box 自动接口绑定，避免 SO_BINDTODEVICE 把连接重新锁回主 WAN；
   防环依赖 socket mark 在 HomeProxy output 链提前放行。

不修改校园认证、原 LAN 规则、路由表、已有硬件限速和 HNAT 开关。
这是多连接 IPv4 分流，不是单连接叠加。TUN 和加密代理本身仍由 CPU 处理。
校园三身份没有对应 IPv6 策略；本工具不声称对 IPv6 做三路聚合。

## 边界

- DNS 后缀来自原生 `direct_list.txt`，配置更新后调度服务同步 dnsmasq 文件。
- dnsmasq 必须收到客户端查询；外部 DoH、已缓存的地址无法保证即时加入集合。
- 同一 IP 上其他域名也会被放行，这是 IP 层绕过 TUN 的固有限制。
- 集合地址一小时超时；DNS 再查询时重新加入。防火墙重建后需重新查询。
- 不主动清空全局 conntrack/HNAT；旧连接不会因此迁移，新连接按新策略建立。
- 只接管显式标记的 sing-box 连接，不接管校园认证、SSH 或光猫管理连接。
- 单个长期复用的代理隧道仍只走一路；多个隧道才可能利用多路。
- 暂仅支持核对过的 HomeProxy TUN 模式，安装前必须检查生成器和防火墙模板。

## 部署与回滚

源码部署至 `/lotusemmc/homeproxy-multiwan`。备份生成器、防火墙模板及真实配置至
本机私有目录（700/600），不要提交凭据、节点或真实设备配置。
原生成器保留为 `generate_client.lotus-multiwan-original.uc`，包装生成器在原生生成后
调用 `integration.py finalize`，验证 sing-box 配置后原子替换。
`patch_template` 向原模板增加专用 mark 放行及动态 DNS 集合，不覆盖其他直连规则。
安装 `homeproxy-multiwan.init`，启动 output 策略后再重启 HomeProxy；必须验证新建
TCP/UDP 连接的三种标记、三种真实源地址，以及直连流量的 HNAT BIND。

回滚：停用新调度服务，恢复原生成器与防火墙模板，移除本工具的 dnsmasq 片段，
重启 HomeProxy 后删除且仅删除 `ip lotus_proxy_multiwan` 表。
软件包升级可能替换包装生成器或模板，需要重新核对，不盲目覆盖新版文件。

验证：`python -m unittest discover -s tools/homeproxy_multiwan/tests -v`
