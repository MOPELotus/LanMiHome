# HomeProxy 倍率阶梯

控制面工具，不转发业务流量，不修改校园三路分流、国内直连规则或 HNAT。

- 排除名称含港澳台地区标记的节点以及流量/到期提示项。
- 所有其他受支持订阅节点均参与，不因某次失败永久删除。
- 从名称解析 `×0.1` 等倍率；未标倍率按 1，末尾 `+3` 按 3。
- 按倍率从低到高选择；同倍率比较 HTTPS 探测延迟，不把倍率折算为延迟。
- 每分钟一轮，最多同时四个探测，每个 4 秒；故障时选择仍可用的最低倍率。
- 恢复低倍率需要连续两轮成功；同倍率至少快 50ms 且连续两轮成功才切换。
- 已有连接不主动中断；全部失败保留最后选择，绝不回退为代理流量直连。
- 这是 HTTPS/TCP 健康检测，不保证 UDP 或单独站点的可用性。

## 部署约定

源文件部署到 `/lotusemmc/homeproxy-tiers`，目录权限 700。
本机生成 `private.json`（600），包含随机 `secret`、空闲回环端口 `port` 和
`probe_url`（例如 `https://www.gstatic.com/generate_204`），不得提交。

完整备份 `/etc/config/homeproxy`、原生生成器和服务启用状态。
保留原生生成器为 `/etc/homeproxy/scripts/generate_client.lotus-original.uc`，
将本目录 `generate_client.uc` 安装至原生成器位置。
原生配置生成前同步当前订阅节点清单，生成后只替换代理选择组并增加带认证的回环控制器。
新配置先通过 `sing-box check`，再原子替换；订阅更新后重新生成阶梯。

设置 `homeproxy.config.lotus_tiers_enabled=1`、`main_node=urltest` 和 `main_udp_node=same`，
安装并启用 `homeproxy-tiers.init` 为 `/etc/init.d/homeproxy-tiers`，验证后启动。
私有备份、节点池、认证信息不进入 Git。

状态位于 `/tmp/homeproxy-tiers-state.json`。关闭主节点会暂停调度；如需改回手动选择，
先将 `lotus_tiers_enabled` 设为 0，停用调度服务，再保存普通 HomeProxy 设置。
HomeProxy 软件包升级后需核对生成器包装是否仍存在，不盲目覆盖新版原生生成器。
回滚时停用调度服务，恢复原生生成器和已备份的 UCI 配置，再重启 HomeProxy。

## 验证

`python -m unittest discover -s tools/homeproxy_tiers/tests -v`
