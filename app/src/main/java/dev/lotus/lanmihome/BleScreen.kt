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
            delay(1000)
        }
    }

    val now = System.currentTimeMillis()
    fun age(ms: Long): String = if (ms <= 0) "从未收到" else "${((now - ms).coerceAtLeast(0) / 1000)} 秒前"
    fun time(ms: Long): String = if (ms <= 0) "—" else DateFormat.getTimeInstance(DateFormat.MEDIUM).format(Date(ms))

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Text("BLE Gateway 诊断", style = MaterialTheme.typography.headlineSmall)
        Text(
            "只被动监听 Xiaomi MiBeacon (FE95)，不会连接温湿度计，也不需要按设备按钮。",
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
                Text("米家智能温湿度计 3 mini", style = MaterialTheme.typography.titleMedium)
                Text("型号：MJWSD06MMC / product id 21941 (0x55B5)")
                Text("命中广播：${state.sensorPackets}")
                Text("最后命中：${age(state.sensorLastSeenMs)}  (${time(state.sensorLastSeenMs)})")
                Text("RSSI：${state.sensorLastRssi?.let { "$it dBm" } ?: "—"}")
                if (state.sensorLastRaw.isNotBlank()) {
                    Text("最近原始 FE95 数据：")
                    Text(state.sensorLastRaw, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                }
                if (state.sensorPackets > 0) {
                    Text(
                        "已确认：在未建立 GATT 连接的情况下收到了该型号的 MiBeacon 广播。",
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("全部 FE95 诊断", style = MaterialTheme.typography.titleMedium)
                Text("FE95 广播总数：${state.totalPackets}")
                Text("最近广播：${age(state.lastSeenMs)}")
                Text("最近 product id：${state.lastProductId?.let { "$it / 0x${it.toString(16).uppercase()}" } ?: "—"}")
                Text("最近 RSSI：${state.lastRssi?.let { "$it dBm" } ?: "—"}")
                if (state.lastRaw.isNotBlank()) {
                    Text(state.lastRaw, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = {
                BleGateway.clear(context)
                localError = null
                state = BleGateway.readState(context)
            }) { Text("清空计数") }
        }

        Text(
            "测试建议：开始扫描后不要按温湿度计按钮，锁屏放置 10～20 分钟，再回来查看“命中广播”和最后命中时间。",
            style = MaterialTheme.typography.bodySmall
        )
    }
}
