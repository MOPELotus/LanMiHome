# 光猫 Wi-Fi 定时控制

这是路由器上独立运行的 Python 3 标准库脚本，不属于手机 App，不改变路由器 Wi-Fi、WAN、认证、分流或硬件加速。

当前只验证华为 OptiXstar HN8346X6-C / V5R022C00S030 的联通 Web 界面。通过已连接 WAN 的 IPv6 link-local 地址管理，不依赖电脑上的 SSH 转发。其他机型或固件拒绝写入。

## 调度与安全

- 默认北京时间每天 23:15 关闭两频、06:10 开启两频。
- cron 每分钟调用 `reconcile`，同一时段完成后只读取本地状态，不再次访问光猫。正常一天只执行两次控制。
- 首次运行、路由器重启或 `/tmp` 状态丢失时，根据当前时段补执行。失败间隔五分钟重试；文件锁防止并发操作。
- 只更改两个 Radio 的 Enable 字段，不更改 SSID、密码、功率、信道、VLAN 或上行线路；每频独立读回，再确认两频最终状态。
- 失败不写成功状态；日志不输出凭据、Cookie、令牌或页面全文。凭据只放路由器 root 私有文件，不提交到 Git。
- 这是按时间点执行，不是持续禁止手动开关；同一时段内的人工调整不会每分钟被覆盖。
- IPv6 link-local Web 使用局域网 HTTP，不适用于不可信或有旁路监听者的二层网络。不要将控制接口向公网开放。

## 部署

将脚本放入路由器私有目录，并在该目录创建 `config.json`，目录权限 `700`、配置权限 `600`。示例仅为字段说明，必须填入自己的设备和凭据：

```json
{
  "host": "fe80::1",
  "interface": "REPLACE_WITH_WAN_DEVICE",
  "username": "REPLACE_WITH_USERNAME",
  "password": "REPLACE_WITH_PASSWORD",
  "on_at": "06:10",
  "off_at": "23:15"
}
```

命令：

```sh
python3 modem_wifi_schedule.py --config /private/path/config.json status
python3 modem_wifi_schedule.py --config /private/path/config.json reconcile
```

`set on` / `set off` 为人工即时控制；`--force` 会在状态已经一致时仍提交，用于验证写入接口。人工测试不会写调度完成标记。

在现有 root crontab 中新增一条（保留其他行），确保 cron 服务启用：

```cron
* * * * * /usr/bin/python3 /private/path/modem_wifi_schedule.py --config /private/path/config.json reconcile 2>&1 | logger -t modem-wifi-schedule
```

停用时删除这条调度，不删除或清空其他 cron 任务。脚本不使用“维护结束”接口，也不会主动更改任何登录密码。

## 测试

```sh
python -m unittest discover -s tools/modem_wifi_schedule/tests -v
```

所有测试使用虚构数据，不连接真实设备。

## 主网段免登录 Web 入口

`modem_wifi_proxy.py` 提供独立的光猫后台代理，不依赖电脑 SSH 隧道。将 `cucc-index.html` 部署为 `/www/cucc/index.html`，访问路由器的 `/cucc` 后会跳转到同主机的 `18081` 端口。这样保留光猫旧界面的根路径、脚本、图片及 iframe，避免与 LuCI 冲突。

在路由器私有配置中增加 `web_proxy`：`bind` 为主 LAN 的明确 IPv4 地址，`port` 为 `18081`，`allowed_networks` 仅含允许直接获得光猫超管权限的主网段，`allowed_hosts` 仅含路由器本地域名和主 LAN 地址。不使用 `0.0.0.0`，不允许访客网或校园 WAN。

将 `modem-wifi-proxy.init` 安装至 `/etc/init.d/modem-wifi-proxy`，添加可执行权限后 enable/start。停用服务并删除 `/www/cucc` 入口即可撤回免登录访问，不影响已有定时任务。

代理只在服务器内保存光猫凭据和认证 Cookie，不把凭据写入网页或重定向 URL。来源网段、Host、Origin 和跨站请求均校验；页面禁止被其他来源嵌入。浏览器访问的设备信息等仍属于超管权限数据，因此不要对不可信的主网段设备开放此服务。

GET 会在光猫会话过期后重新登录；POST 不会在会话过期后自动重放，页面提示刷新后由用户重试。与定时控制共用操作锁，避免计划任务和页面操作同时修改无线设置。代理不自动修改任何光猫配置；只有用户在后台主动提交的请求才转发。
