package dev.lotus.lanmihome

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.weight
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
    var error by remember { mutableStateOf<String?>(null) }

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
        onSave(
            initial.copy(
                manageHotspot = manageHotspot,
                ssid = ssid.trim(),
                passphrase = nextPassword,
                interfaceName = interfaceName.trim(),
                port = parsedPort,
                fanToken = nextFan ?: "",
                lampToken = nextLamp ?: "",
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
                Text(
                    "热点使用 WPA2-Personal / 2.4 GHz；SSID 和密码需要与要接管的原 AP 一致。",
                    style = MaterialTheme.typography.bodySmall,
                )
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = { Button(onClick = { save() }) { Text("保存") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}
