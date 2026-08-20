package dev.lotus.lanmihome

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

@Composable
fun NightNodeScreen(onUseLocal: () -> Unit) {
    val context = LocalContext.current
    var status by remember { mutableStateOf(NightNodeRuntime.snapshot()) }
    var config by remember { mutableStateOf(NightNodePrefs.read(context)) }
    var dialog by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        while (true) {
            status = NightNodeRuntime.snapshot()
            delay(1000)
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("夜间节点", style = MaterialTheme.typography.headlineSmall)
        Text(
            "给有 root 的备用 Android 手机使用：它可以用同 SSID 的 2.4 GHz WPA2 热点接管风扇/台灯，直接通过 miIO UDP 54321 控制，并在本机 :${config.port} 暴露兼容 LanMiHome API。",
            style = MaterialTheme.typography.bodySmall,
        )

        Section("运行状态") {
            ToggleLine("Night Node 服务", status.running, enabled = false) { }
            Text("Root：${if (status.root) "可用" else "未确认"}")
            Text("热点：${status.hotspotInterface ?: "未发现"} · ${status.hotspotAddress ?: "—"}")
            Text("HTTP：${if (status.running) "0.0.0.0:${status.serverPort}" else "未运行"}")
            status.lastError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = {
                        NightNodeService.start(context)
                        status = NightNodeRuntime.snapshot()
                    },
                    modifier = Modifier.weight(1f),
                    enabled = !status.running,
                ) { Text("启动") }
                OutlinedButton(
                    onClick = { NightNodeService.stop(context) },
                    modifier = Modifier.weight(1f),
                    enabled = status.running,
                ) { Text("停止") }
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(
                    onClick = { NightNodeService.discover(context) },
                    modifier = Modifier.weight(1f),
                    enabled = status.running,
                ) { Text("重新发现") }
                OutlinedButton(
                    onClick = onUseLocal,
                    modifier = Modifier.weight(1f),
                    enabled = status.running,
                ) { Text("本机控制") }
            }
        }

        Section("晨间交接") {
            Text("状态：${handoffStateLabel(status)}")
            status.handoffReason?.let { Text("原因：$it") }
            if (status.handoffWifiHits > 0) {
                Text("Wi-Fi 确认：${status.handoffWifiHits}/${config.handoffConfirmScans}")
            }
            if (status.handoffSeenSsids.isNotEmpty()) {
                Text("已看到：${status.handoffSeenSsids.joinToString(", ")}")
            }
            status.handoffDeadlineMillis?.let { deadline ->
                val remain = ((deadline - System.currentTimeMillis()).coerceAtLeast(0L) + 999L) / 1000L
                if (status.handoffState == "countdown") Text("默认确认：${remain}s 后交接")
            }
            Text(
                "${config.handoffWindowStart}–${config.handoffWindowEnd} 生效；充电可触发 ${config.handoffChargeDelaySeconds}s 倒计时。${config.handoffScanInterface} 连续 ${config.handoffConfirmScans} 次看到 ${config.handoffSsids} 中任一 SSID，可独立触发并缩短为 ${config.handoffWifiDelaySeconds}s。",
                style = MaterialTheme.typography.bodySmall,
            )
            OutlinedButton(
                onClick = { NightNodeService.testHandoff(context) },
                enabled = status.running && status.root,
            ) { Text("测试晨间交接（忽略时段）") }
            Text(
                "测试会真实执行交接：若当前能扫描到目标 SSID，会弹出默认确认界面并在 ${config.handoffWifiDelaySeconds}s 后停止 Night Node / SoftAP；可在弹窗中立即交接、延后 5 分钟或取消本次。",
                style = MaterialTheme.typography.bodySmall,
            )
        }

        Section("接管配置") {
            Text("SSID：${config.ssid.ifBlank { "未配置" }}")
            Text("热点：${if (config.manageHotspot) "由 App/root 管理" else "使用已经开启的热点"}")
            Text("接口：${config.interfaceName.ifBlank { "自动识别" }}")
            Text("风扇 token：${if (normalizeMiioToken(config.fanToken) != null) "已配置" else "未配置"}")
            Text("台灯 token：${if (normalizeMiioToken(config.lampToken) != null) "已配置" else "未配置"}")
            OutlinedButton(onClick = { dialog = true }) { Text("编辑夜间节点配置") }
            Text(
                "密码和 Xiaomi token 只保存在这台手机的私有 SharedPreferences；界面不会回显已有 secret。App 已关闭 Android 备份，避免这些值进入系统备份。",
                style = MaterialTheme.typography.bodySmall,
            )
        }

        Section("设备") {
            Text("风扇：${status.fanIp ?: "等待发现"}")
            Text("台灯：${status.lampIp ?: if (normalizeMiioToken(config.lampToken) == null) "未配置" else "等待发现"}")
            Text(
                "发现过程先读取热点邻居表，再向热点子网发送标准 miIO hello；使用对应 token 成功读取 MIoT 2/1 后才确认设备。IP 改变时会重新发现。",
                style = MaterialTheme.typography.bodySmall,
            )
        }

        Section("日志") {
            if (status.logs.isEmpty()) {
                Text("暂无日志", style = MaterialTheme.typography.bodySmall)
            } else {
                Text(
                    status.logs.takeLast(60).joinToString("\n"),
                    fontFamily = FontFamily.Monospace,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }

    if (dialog) {
        NightNodeConfigDialog(
            initial = config,
            onDismiss = { dialog = false },
            onSave = { newConfig ->
                NightNodePrefs.save(context, newConfig)
                config = NightNodePrefs.read(context)
                val wasRunning = NightNodeRuntime.snapshot().running
                if (wasRunning) NightNodeService.restart(context)
                dialog = false
            },
        )
    }
}

private fun handoffStateLabel(status: NightNodeStatus): String = when (status.handoffState) {
    "countdown" -> "倒计时"
    "delayed" -> "已延后"
    "cancelled" -> "本次已取消"
    "executing" -> "正在交接"
    else -> "等待触发"
}

@Composable
private fun NightNodeConfigDialog(
    initial: NightNodeConfig,
    onDismiss: () -> Unit,
    onSave: (NightNodeConfig) -> Unit,
) {
    var manageHotspot by remember(initial) { mutableStateOf(initial.manageHotspot) }
    var ssid by remember(initial) { mutableStateOf(initial.ssid) }
    var passphrase by remember(initial) { mutableStateOf("") }
    var interfaceName by remember(initial) { mutableStateOf(initial.interfaceName) }
    var port by remember(initial) { mutableStateOf(initial.port.toString()) }
    var fanToken by remember(initial) { mutableStateOf("") }
    var lampToken by remember(initial) { mutableStateOf("") }
    var handoffEnabled by remember(initial) { mutableStateOf(initial.handoffEnabled) }
    var handoffWindowStart by remember(initial) { mutableStateOf(initial.handoffWindowStart) }
    var handoffWindowEnd by remember(initial) { mutableStateOf(initial.handoffWindowEnd) }
    var handoffScanInterface by remember(initial) { mutableStateOf(initial.handoffScanInterface) }
    var handoffSsids by remember(initial) { mutableStateOf(initial.handoffSsids) }
    var handoffChargeDelay by remember(initial) { mutableStateOf(initial.handoffChargeDelaySeconds.toString()) }
    var handoffWifiDelay by remember(initial) { mutableStateOf(initial.handoffWifiDelaySeconds.toString()) }
    var error by remember { mutableStateOf<String?>(null) }

    fun validClock(value: String): Boolean = Regex("^(?:[01]\\d|2[0-3]):[0-5]\\d$").matches(value)

    fun save() {
        val parsedPort = port.toIntOrNull()
        if (parsedPort == null || parsedPort !in 1024..65535) {
            error = "端口必须在 1024–65535"
            return
        }
        if (manageHotspot && ssid.isBlank()) {
            error = "SSID 不能为空"
            return
        }
        val nextPassword = if (passphrase.isBlank()) initial.passphrase else passphrase
        if (manageHotspot && nextPassword.length < 8) {
            error = "WPA2 密码至少 8 位；留空表示保留已保存密码"
            return
        }
        val nextFan = if (fanToken.isBlank()) initial.fanToken else normalizeMiioToken(fanToken)
        if (fanToken.isNotBlank() && nextFan == null) {
            error = "风扇 token 应为 16 字节 / 32 位十六进制"
            return
        }
        val nextLamp = if (lampToken.isBlank()) initial.lampToken else normalizeMiioToken(lampToken)
        if (lampToken.isNotBlank() && nextLamp == null) {
            error = "台灯 token 应为 16 字节 / 32 位十六进制"
            return
        }
        if (!validClock(handoffWindowStart) || !validClock(handoffWindowEnd)) {
            error = "晨间交接时段必须为 HH:mm"
            return
        }
        if (handoffScanInterface.isBlank()) {
            error = "扫描接口不能为空"
            return
        }
        if (handoffSsids.split(',').map(String::trim).none(String::isNotBlank)) {
            error = "至少配置一个交接目标 SSID"
            return
        }
        val chargeDelay = handoffChargeDelay.toIntOrNull()
        val wifiDelay = handoffWifiDelay.toIntOrNull()
        if (chargeDelay == null || chargeDelay !in 15..1800 || wifiDelay == null || wifiDelay !in 5..300) {
            error = "交接延迟范围：充电 15–1800 秒，Wi-Fi 5–300 秒"
            return
        }
        onSave(
            initial.copy(
                manageHotspot = manageHotspot,
                ssid = ssid.trim(),
                passphrase = nextPassword,
                interfaceName = interfaceName.trim(),
                port = parsedPort,
                fanToken = nextFan ?: "",
                lampToken = nextLamp ?: "",
                handoffEnabled = handoffEnabled,
                handoffWindowStart = handoffWindowStart,
                handoffWindowEnd = handoffWindowEnd,
                handoffScanInterface = handoffScanInterface.trim(),
                handoffSsids = handoffSsids.split(',').map(String::trim).filter(String::isNotBlank).joinToString(","),
                handoffChargeDelaySeconds = chargeDelay,
                handoffWifiDelaySeconds = wifiDelay,
            )
        )
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Night Node 配置") },
        text = {
            Column(
                Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(9.dp),
            ) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("由 App 启停热点")
                    Switch(checked = manageHotspot, onCheckedChange = { manageHotspot = it })
                }
                OutlinedTextField(ssid, { ssid = it }, label = { Text("SSID") }, singleLine = true)
                OutlinedTextField(
                    passphrase,
                    { passphrase = it },
                    label = { Text(if (initial.passphrase.isBlank()) "WPA2 密码" else "WPA2 密码（留空保持现有）") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                )
                OutlinedTextField(
                    interfaceName,
                    { interfaceName = it },
                    label = { Text("热点接口（留空自动；10S 实测 wlan1）") },
                    singleLine = true,
                )
                OutlinedTextField(
                    port,
                    { port = it.filter(Char::isDigit) },
                    label = { Text("HTTP 端口") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                )
                HorizontalDivider()
                OutlinedTextField(
                    fanToken,
                    { fanToken = it },
                    label = { Text(if (normalizeMiioToken(initial.fanToken) != null) "风扇 token（留空保持现有）" else "风扇 token") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii),
                )
                OutlinedTextField(
                    lampToken,
                    { lampToken = it },
                    label = { Text(if (normalizeMiioToken(initial.lampToken) != null) "台灯 token（留空保持现有）" else "台灯 token（可选）") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Ascii),
                )
                HorizontalDivider()
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("启用晨间交接")
                    Switch(checked = handoffEnabled, onCheckedChange = { handoffEnabled = it })
                }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(handoffWindowStart, { handoffWindowStart = it }, label = { Text("开始") }, modifier = Modifier.weight(1f), singleLine = true)
                    OutlinedTextField(handoffWindowEnd, { handoffWindowEnd = it }, label = { Text("结束") }, modifier = Modifier.weight(1f), singleLine = true)
                }
                OutlinedTextField(handoffScanInterface, { handoffScanInterface = it }, label = { Text("Wi-Fi 扫描接口") }, singleLine = true)
                OutlinedTextField(handoffSsids, { handoffSsids = it }, label = { Text("主路由 SSID（逗号分隔）") }, singleLine = true)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        handoffChargeDelay,
                        { handoffChargeDelay = it.filter(Char::isDigit) },
                        label = { Text("充电延迟/秒") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    )
                    OutlinedTextField(
                        handoffWifiDelay,
                        { handoffWifiDelay = it.filter(Char::isDigit) },
                        label = { Text("Wi-Fi 延迟/秒") },
                        modifier = Modifier.weight(1f),
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    )
                }
                Text(
                    "热点使用 WPA2-Personal / 2.4 GHz；SSID 和密码需要与要接管的原 AP 一致。晨间交接默认连续扫描 2 次确认目标 Wi-Fi；充电和 Wi-Fi 任一条件都可触发。",
                    style = MaterialTheme.typography.bodySmall,
                )
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = { Button(onClick = { save() }) { Text("保存") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}
