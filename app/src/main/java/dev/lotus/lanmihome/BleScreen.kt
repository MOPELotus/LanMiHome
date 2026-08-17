package dev.lotus.lanmihome

import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import java.text.DateFormat
import java.util.Date

@Composable
fun SensorScreen(server: SensorState?) {
    val context = LocalContext.current
    var local by remember { mutableStateOf(BleGateway.readState(context)) }
    var localError by remember { mutableStateOf<String?>(null) }
    var keyDialog by remember { mutableStateOf(false) }

    val notificationLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        val ok = BleGateway.hasPermissions(context)
        if (ok) {
            localError = BleGateway.start(context)
            BleGateway.notificationPermission()?.let { perm ->
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && context.checkSelfPermission(perm) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                    notificationLauncher.launch(perm)
                }
            }
        } else {
            localError = "蓝牙扫描/定位权限未完整授予"
        }
        local = BleGateway.readState(context)
    }

    LaunchedEffect(Unit) {
        while (true) {
            local = BleGateway.readState(context)
            delay(1000)
        }
    }

    val now = System.currentTimeMillis()
    fun age(ms: Long): String = if (ms <= 0) "从未" else "${((now - ms).coerceAtLeast(0) / 1000)} 秒前"
    fun time(ms: Long): String = if (ms <= 0) "—" else DateFormat.getTimeInstance(DateFormat.MEDIUM).format(Date(ms))
    fun value(v: Double?, suffix: String) = v?.let { "%.1f%s".format(it, suffix) } ?: "—"

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("温湿度计", style = MaterialTheme.typography.headlineSmall)

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("RAX3000M 上的最新数据", style = MaterialTheme.typography.titleMedium)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Column { Text("温度", style=MaterialTheme.typography.labelMedium); Text(value(server?.temperature, "°C"), style=MaterialTheme.typography.headlineMedium) }
                    Column { Text("湿度", style=MaterialTheme.typography.labelMedium); Text(value(server?.humidity, "%"), style=MaterialTheme.typography.headlineMedium) }
                    Column { Text("电量", style=MaterialTheme.typography.labelMedium); Text(server?.battery?.let { "$it%" } ?: "—", style=MaterialTheme.typography.headlineMedium) }
                }
                Text(if (server?.available == true) "服务端状态：在线" else "服务端状态：暂无新数据")
                server?.ageSeconds?.let { Text("服务端最后收到：${it} 秒前") }
                server?.mac?.let { Text("设备：$it") }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                Text("小米 10S BLE 网关", style = MaterialTheme.typography.titleMedium)
                Text("服务：${if(local.running) "运行中" else "未运行"} · 扫描：${if(local.scanning) "进行中" else "未扫描"}")
                Text("BLE Key：${if(local.bindKeyConfigured) "已配置" else "未配置"}")
                local.lastError?.let { Text("扫描错误：$it") }
                localError?.let { Text("操作结果：$it") }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = {
                        if (BleGateway.hasPermissions(context)) {
                            localError = BleGateway.start(context)
                            BleGateway.notificationPermission()?.let { perm ->
                                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && context.checkSelfPermission(perm) != android.content.pm.PackageManager.PERMISSION_GRANTED) notificationLauncher.launch(perm)
                            }
                        } else permissionLauncher.launch(BleGateway.requiredPermissions())
                    }) { Text(if(local.running) "重新启动网关" else "启动网关") }
                    OutlinedButton(onClick = { BleGateway.stop(context); local = BleGateway.readState(context) }) { Text("停止") }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { keyDialog = true }) { Text(if(local.bindKeyConfigured) "更换 BLE Key" else "配置 BLE Key") }
                    if (local.bindKeyConfigured) OutlinedButton(onClick = { BleGateway.clearBindKey(context); local = BleGateway.readState(context) }) { Text("删除 Key") }
                }
                Text("Key 只保存在这台手机本地，不会上传到 RAX3000M。", style=MaterialTheme.typography.bodySmall)
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("本机解密状态", style = MaterialTheme.typography.titleMedium)
                Text("目标：MJWSD06MMC / 0x55B5")
                Text("命中广播：${local.sensorPackets} · 解密成功：${local.decryptOk} · 失败：${local.decryptFail}")
                Text("本机温度：${value(local.temperature, "°C")} · 湿度：${value(local.humidity, "%")} · 电量：${local.battery?.let{"$it%"} ?: "—"}")
                Text("最后广播：${age(local.lastSeenMs)} (${time(local.lastSeenMs)}) · RSSI：${local.rssi?.let{"$it dBm"} ?: "—"}")
                local.mac?.let { Text("MAC：$it") }
                Text("最后有效测量：${age(local.lastMeasurementMs)}")
                Text("最后上传：${age(local.lastUploadMs)}")
                local.lastUploadError?.let { Text("上传错误：$it") }
            }
        }

        if (local.lastRaw.isNotBlank()) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("诊断", style = MaterialTheme.typography.titleMedium)
                    Text("最近 FE95：", style=MaterialTheme.typography.bodySmall)
                    Text(local.lastRaw, fontFamily=FontFamily.Monospace, style=MaterialTheme.typography.bodySmall)
                    if(local.lastPlain.isNotBlank()) {
                        Text("最近解密明文：", style=MaterialTheme.typography.bodySmall)
                        Text(local.lastPlain, fontFamily=FontFamily.Monospace, style=MaterialTheme.typography.bodySmall)
                    }
                    OutlinedButton(onClick={BleGateway.clearStats(context)}) { Text("清空诊断计数") }
                }
            }
        }

        Text("网关使用前台服务持续扫描；锁屏后也应继续工作。HyperOS 若仍杀后台，再把本应用设为“无限制/允许自启动”。", style=MaterialTheme.typography.bodySmall)
    }

    if (keyDialog) {
        BindKeyDialog(
            configured = local.bindKeyConfigured,
            onDismiss = { keyDialog = false },
            onSave = { raw ->
                if (BleGateway.saveBindKey(context, raw)) {
                    keyDialog = false
                    localError = "BLE Key 已保存"
                    local = BleGateway.readState(context)
                } else localError = "BLE Key 应为 16 字节 / 32 位十六进制"
            }
        )
    }
}

@Composable
private fun BindKeyDialog(configured: Boolean, onDismiss: () -> Unit, onSave: (String) -> Unit) {
    var value by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("BLE Bind Key") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(if(configured) "已配置 Key。输入新的 32 位十六进制 Key 可替换。" else "输入 Xiaomi Cloud Tokens Extractor 得到的 BLE Key。")
                OutlinedTextField(
                    value=value,
                    onValueChange={value=it},
                    label={Text("32 位十六进制")},
                    singleLine=true,
                    visualTransformation=PasswordVisualTransformation(),
                    keyboardOptions=KeyboardOptions(keyboardType=KeyboardType.Ascii),
                )
                Text("不会显示或上传现有 Key。", style=MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton={Button(onClick={onSave(value)}){Text("保存")}},
        dismissButton={TextButton(onClick=onDismiss){Text("取消")}},
    )
}
