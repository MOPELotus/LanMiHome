package dev.lotus.lanmihome

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import java.text.DateFormat
import java.util.Date

@Composable
fun BleScreen() {
    val context = LocalContext.current
    var state by remember { mutableStateOf(BleGateway.readState(context)) }
    var permissionOk by remember { mutableStateOf(BleGateway.hasPermissions(context)) }
    var localError by remember { mutableStateOf<String?>(null) }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        permissionOk = grants.values.all { it } && BleGateway.hasPermissions(context)
        if (permissionOk) localError = BleGateway.start(context)
        else localError = "权限未授予，无法扫描 BLE"
        state = BleGateway.readState(context)
    }

    LaunchedEffect(Unit) {
        while (true) {
            permissionOk = BleGateway.hasPermissions(context)
            state = BleGateway.readState(context)
            delay(500)
        }
    }

    val now = System.currentTimeMillis()
    fun age(ms: Long): String = if (ms <= 0) "从未收到" else "${((now - ms).coerceAtLeast(0) / 1000)} 秒前"
    fun time(ms: Long): String = if (ms <= 0) "—" else DateFormat.getTimeInstance(DateFormat.MEDIUM).format(Date(ms))

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("BLE Gateway 诊断 v3", style = MaterialTheme.typography.headlineSmall)
        Text(
            "本版先无过滤扫描附近全部 BLE，再从原始广告中查找 Xiaomi FE95。保持此页面亮屏测试即可。",
            style = MaterialTheme.typography.bodyMedium
        )

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("扫描状态", style = MaterialTheme.typography.titleMedium)
                Text(if (state.enabled) "已启动" else "未启动")
                Text(if (permissionOk) "权限：已授予" else "权限：需要蓝牙扫描 + 定位")
                state.lastError?.let { Text("系统错误：$it") }
                localError?.let { Text("操作结果：$it") }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(onClick = {
                        localError = null
                        if (BleGateway.hasPermissions(context)) {
                            localError = BleGateway.start(context)
                            state = BleGateway.readState(context)
                        } else {
                            permissionLauncher.launch(BleGateway.requiredPermissions())
                        }
                    }) { Text(if (state.enabled) "重新启动扫描" else "开始扫描") }
                    OutlinedButton(onClick = {
                        localError = BleGateway.stop(context)
                        state = BleGateway.readState(context)
                    }, enabled = permissionOk) { Text("停止") }
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("全部 BLE 原始扫描", style = MaterialTheme.typography.titleMedium)
                Text("收到 BLE 广播：${state.allPackets}")
                Text("最近广播：${age(state.anyLastSeenMs)}  (${time(state.anyLastSeenMs)})")
                Text("最近 RSSI：${state.anyLastRssi?.let { "$it dBm" } ?: "—"}")
                if (state.anyLastRaw.isNotBlank()) {
                    Text("最近完整广告包：")
                    Text(state.anyLastRaw, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                }
                when {
                    state.allPackets == 0L -> Text("如果保持亮屏扫描 30 秒仍为 0，问题在 Android 扫描/权限层，不是温湿度计。")
                    state.totalPackets == 0L -> Text("BLE 扫描正常，但暂未发现 FE95；继续看温湿度计是否会周期广播。")
                    else -> Text("已收到 Xiaomi FE95，可以继续判断设备型号。")
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("全部 Xiaomi FE95", style = MaterialTheme.typography.titleMedium)
                Text("FE95 广播总数：${state.totalPackets}")
                Text("最近广播：${age(state.lastSeenMs)}")
                Text("最近 product id：${state.lastProductId?.let { "$it / 0x${it.toString(16).uppercase()}" } ?: "—"}")
                Text("最近 RSSI：${state.lastRssi?.let { "$it dBm" } ?: "—"}")
                if (state.lastRaw.isNotBlank()) {
                    Text("最近 FE95 payload：")
                    Text(state.lastRaw, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("米家智能温湿度计 3 mini", style = MaterialTheme.typography.titleMedium)
                Text("预期型号：MJWSD06MMC / product id 21941 (0x55B5)")
                Text("命中广播：${state.sensorPackets}")
                Text("最后命中：${age(state.sensorLastSeenMs)}  (${time(state.sensorLastSeenMs)})")
                Text("RSSI：${state.sensorLastRssi?.let { "$it dBm" } ?: "—"}")
                if (state.sensorLastRaw.isNotBlank()) {
                    Text("最近原始 FE95 数据：")
                    Text(state.sensorLastRaw, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        OutlinedButton(onClick = {
            BleGateway.clear(context)
            localError = null
            state = BleGateway.readState(context)
        }) { Text("清空计数") }

        Text(
            "这版第一步不用等 5～15 分钟：在家里正常 BLE 环境下，无过滤扫描通常几秒内就应该看到“收到 BLE 广播”增长。先用它确认 Android 扫描链路。",
            style = MaterialTheme.typography.bodySmall
        )
    }
}
