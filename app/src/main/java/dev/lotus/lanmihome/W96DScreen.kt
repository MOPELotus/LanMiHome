package dev.lotus.lanmihome

import android.Manifest
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.math.roundToInt

@Composable
internal fun W96DScreen(
    activeBase: String,
    primaryBase: String,
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val controller = remember { W96DController(context) }
    val ble = remember { W96DBleClient.get(context) }
    val scope = rememberCoroutineScope()
    val snack = remember { SnackbarHostState() }

    var state by remember { mutableStateOf<W96DState?>(null) }
    var busy by remember { mutableStateOf(false) }
    var speedDraft by remember { mutableFloatStateOf(35f) }
    var speedDragging by remember { mutableStateOf(false) }

    suspend fun refresh(showError: Boolean = false) {
        val next = controller.refresh(activeBase, primaryBase)
        state = next
        if (!speedDragging && next.speed != null) speedDraft = next.speed.toFloat()
        if (showError && !next.available && !next.error.isNullOrBlank()) snack.showSnackbar(next.error)
    }

    fun patch(vararg values: Pair<String, Any>) {
        scope.launch {
            busy = true
            try {
                state = controller.patch(activeBase, primaryBase, mapOf(*values))
                state?.speed?.let { speedDraft = it.toFloat() }
            } catch (e: Exception) {
                snack.showSnackbar("W96D 操作失败：${e.message}")
            } finally {
                busy = false
            }
        }
    }

    fun connectDirect() {
        scope.launch {
            busy = true
            try {
                state = controller.connectDirect()
                state?.speed?.let { speedDraft = it.toFloat() }
                snack.showSnackbar("已连接 W96D 本机蓝牙")
            } catch (e: Exception) {
                snack.showSnackbar("蓝牙连接失败：${e.message}")
            } finally {
                busy = false
            }
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { grants ->
        if (grants.values.all { it }) connectDirect()
        else scope.launch { snack.showSnackbar("需要蓝牙扫描和连接权限") }
    }

    fun requestDirectConnection() {
        if (ble.hasRuntimePermissions()) {
            connectDirect()
        } else {
            val permissions = if (Build.VERSION.SDK_INT >= 31) {
                arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            } else {
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
            }
            permissionLauncher.launch(permissions)
        }
    }

    LaunchedEffect(activeBase, primaryBase) {
        while (isActive) {
            if (!busy && !speedDragging) refresh()
            delay(2500)
        }
    }

    Scaffold(snackbarHost = { SnackbarHost(snack) }) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 14.dp, vertical = 10.dp)
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            W96DStatusCard(state)

            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                        Column(Modifier.weight(1f)) {
                            Text("电源", style = MaterialTheme.typography.titleMedium)
                            Text(
                                when (state?.power) { true -> "运行中"; false -> "已停止（BLE 可保持在线）"; null -> "未知" },
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                        Switch(
                            checked = state?.power == true,
                            enabled = !busy && state?.available == true,
                            onCheckedChange = { patch("power" to it) },
                        )
                    }

                    Text("风速 ${speedDraft.roundToInt()}%", style = MaterialTheme.typography.titleMedium)
                    Slider(
                        value = speedDraft,
                        onValueChange = {
                            speedDragging = true
                            speedDraft = it
                        },
                        onValueChangeFinished = {
                            speedDragging = false
                            patch("speed" to speedDraft.roundToInt())
                        },
                        valueRange = 0f..100f,
                        steps = 99,
                        enabled = !busy && state?.available == true,
                    )
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf(10, 35, 70, 100).forEachIndexed { index, speed ->
                            OutlinedButton(
                                onClick = {
                                    speedDraft = speed.toFloat()
                                    patch("speed" to speed)
                                },
                                enabled = !busy && state?.available == true,
                                modifier = Modifier.weight(1f),
                                contentPadding = PaddingValues(horizontal = 4.dp),
                            ) { Text("${index + 1}档") }
                        }
                    }
                    state?.gearSpeeds?.let { Text("实体四档：${it.joinToString(" / ") { n -> "$n%" }}", style = MaterialTheme.typography.bodySmall) }
                }
            }

            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    W96DToggle("自然风", state?.naturalWind, busy, state?.available == true) { patch("natural_wind" to it) }
                    W96DToggle("Turbo", state?.turbo, busy, state?.available == true) { patch("turbo" to it) }
                    W96DToggle("指示灯", state?.light, busy, state?.available == true) { patch("light" to it) }
                    HorizontalDivider()
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(
                            onClick = { patch("shutdown_delay_seconds" to 0) },
                            enabled = !busy && state?.available == true,
                            modifier = Modifier.weight(1f),
                        ) { Text("BLE 永久在线") }
                        OutlinedButton(
                            onClick = { scope.launch { busy = true; try { refresh(true) } finally { busy = false } } },
                            enabled = !busy,
                            modifier = Modifier.weight(1f),
                        ) { Text("刷新") }
                    }
                }
            }

            W96DTelemetryCard(state)

            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = { requestDirectConnection() },
                    enabled = !busy,
                    modifier = Modifier.weight(1f),
                ) { Text("连接本机蓝牙") }
                OutlinedButton(
                    onClick = {
                        scope.launch {
                            busy = true
                            try {
                                controller.disconnectDirect()
                                refresh()
                            } finally { busy = false }
                        }
                    },
                    enabled = !busy && ble.isConnected,
                    modifier = Modifier.weight(1f),
                ) { Text("释放本机蓝牙") }
            }

            Text(
                "控制优先级：当前 LAN 节点 W96D sidecar → 主路由 sidecar → 当前 Wi‑Fi 网关 sidecar → 已配对的本机 BLE。外出首次使用需点一次“连接本机蓝牙”；之后会记住设备地址。",
                style = MaterialTheme.typography.bodySmall,
            )

            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
        }
    }
}

@Composable
private fun W96DStatusCard(state: W96DState?) {
    val source = when (state?.source) {
        "router-bluez" -> "路由器 BLE"
        "night-node-ble" -> "10S Night Node BLE"
        "phone-ble" -> "本机 BLE（服务端旁路）"
        "none" -> "未连接"
        else -> state?.source ?: "等待刷新"
    }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("W96D", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                AssistChip(onClick = {}, label = { Text(source) })
            }
            Text(
                when {
                    state == null -> "正在探测…"
                    state.available -> "${state.name ?: "W96D"}${state.address?.let { " · $it" } ?: ""}"
                    else -> state.error ?: "不可用"
                },
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
private fun W96DToggle(
    label: String,
    value: Boolean?,
    busy: Boolean,
    available: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, modifier = Modifier.weight(1f))
        Switch(checked = value == true, enabled = !busy && available, onCheckedChange = onChange)
    }
}

@Composable
private fun W96DTelemetryCard(state: W96DState?) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text("实时状态", style = MaterialTheme.typography.titleMedium)
            W96DMetric("电池", state?.batteryVoltage?.let { "%.2f V".format(it) } ?: "--")
            W96DMetric("电池电流", state?.batteryCurrentMa?.let { "$it mA" } ?: "--")
            W96DMetric("电池容量设定", state?.batteryCapacityMwh?.let { "$it mWh" } ?: "--")
            W96DMetric("VBUS", if (state?.vbusVoltage != null) "%.2f V · %d mA".format(state.vbusVoltage, state.vbusCurrentMa ?: 0) else "--")
            W96DMetric("充放电", when (state?.chargeStatus) { 1 -> "充电中"; 0 -> "放电/未充电"; else -> "--" })
            W96DMetric("电机", if (state?.motorPowerW != null) "%.2f W · %d mA".format(state.motorPowerW, state.motorCurrentMa ?: 0) else "--")
            W96DMetric("Turbo剩余", state?.turboRemainingSeconds?.let { "${it}s" } ?: "--")
            W96DMetric("BLE自动关闭", when (state?.shutdownDelaySeconds) { 0 -> "永不"; null -> "--"; else -> "${state.shutdownDelaySeconds}s" })
        }
    }
}

@Composable
private fun W96DMetric(label: String, value: String) {
    Row(Modifier.fillMaxWidth()) {
        Text(label, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
        Text(value, style = MaterialTheme.typography.bodySmall)
    }
}
