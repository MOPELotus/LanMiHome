package dev.lotus.lanmihome

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.isActive
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@Composable
internal fun W96dScreen(primaryBase: String) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val ble = remember { W96dGattClient(context.applicationContext) }
    var environment by remember { mutableStateOf(W96dPrefs.environment(context)) }
    var outdoor by remember { mutableStateOf(W96dPrefs.outdoor(context)) }
    var nightUrl by remember { mutableStateOf(W96dPrefs.nightUrl(context)) }
    var state by remember { mutableStateOf<W96dState?>(null) }
    var busy by remember { mutableStateOf(false) }
    var lastError by remember { mutableStateOf<String?>(null) }
    var speedDraft by remember { mutableIntStateOf(50) }
    var forceOutdoorDialog by remember { mutableStateOf(false) }
    var pendingPermissionOutdoor by remember { mutableStateOf(false) }
    var releaseFailure by remember { mutableStateOf<String?>(null) }

    fun remote(owner: W96dOwner): W96dRemoteClient = when (owner) {
        W96dOwner.ROUTER -> W96dRemoteClient(routerW96dBase(primaryBase))
        W96dOwner.NIGHT_NODE -> W96dRemoteClient(nightW96dBase(context))
        W96dOwner.PHONE -> error("PHONE owner does not use HTTP")
    }

    suspend fun connectPhone() {
        busy = true
        try {
            state = ble.connect()
            speedDraft = state?.speed ?: speedDraft
            lastError = null
        } catch (e: Exception) {
            state = W96dState(owner = "phone", error = e.message)
            lastError = e.message
        } finally {
            busy = false
        }
    }

    suspend fun enterOutdoor(forceWithoutRelease: Boolean = false) {
        val previousOwner = w96dOwner(environment, false)
        busy = true
        try {
            if (!forceWithoutRelease) {
                try {
                    remote(previousOwner).release()
                } catch (e: Exception) {
                    releaseFailure = e.message ?: e.javaClass.simpleName
                    forceOutdoorDialog = true
                    return
                }
            }
            W96dPrefs.setOutdoor(context, true)
            outdoor = true
            lastError = null
        } finally {
            busy = false
        }
        connectPhone()
    }

    suspend fun exitOutdoor() {
        busy = true
        try {
            runCatching { ble.disconnect() }
            W96dPrefs.setOutdoor(context, false)
            outdoor = false
            val owner = w96dOwner(environment, false)
            try {
                remote(owner).resume()
                lastError = null
            } catch (e: Exception) {
                lastError = "已退出外出模式，但 ${w96dOwnerLabel(owner)} 恢复未确认：${e.message}"
            }
            state = null
        } finally {
            busy = false
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { result ->
        val granted = requiredW96dBlePermissions().all {
            result[it] == true || context.checkSelfPermission(it) == android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        if (BuildConfig.NIGHT_NODE_ENABLED && granted) W96dNightService.start(context)
        if (granted && pendingPermissionOutdoor) {
            pendingPermissionOutdoor = false
            scope.launch { enterOutdoor(false) }
        } else if (!granted) {
            pendingPermissionOutdoor = false
            lastError = "需要蓝牙扫描/连接权限才能使用 W96D"
        }
    }

    fun requestBlePermissions(forOutdoor: Boolean) {
        pendingPermissionOutdoor = forOutdoor
        permissionLauncher.launch(requiredW96dBlePermissions())
    }

    fun requestOutdoor() {
        if (!hasW96dBlePermissions(context)) requestBlePermissions(true)
        else scope.launch { enterOutdoor(false) }
    }

    fun command(key: String, value: Any) {
        scope.launch {
            busy = true
            try {
                state = if (outdoor) {
                    if (!hasW96dBlePermissions(context)) throw IllegalStateException("缺少蓝牙权限")
                    ble.patch(key, value)
                } else {
                    remote(w96dOwner(environment, false)).patch(key to value)
                }
                speedDraft = state?.speed ?: speedDraft
                lastError = null
            } catch (e: Exception) {
                lastError = e.message ?: e.javaClass.simpleName
            } finally {
                busy = false
            }
        }
    }

    LaunchedEffect(environment, outdoor, nightUrl, primaryBase) {
        while (isActive) {
            if (!busy) {
                try {
                    val next = if (outdoor) {
                        if (!hasW96dBlePermissions(context)) W96dState(owner = "phone", error = "缺少蓝牙权限")
                        else ble.connect()
                    } else {
                        remote(w96dOwner(environment, false)).state()
                    }
                    state = next
                    next.speed?.let { speedDraft = it }
                    if (next.error == null) lastError = null
                } catch (e: Exception) {
                    lastError = e.message ?: e.javaClass.simpleName
                    state = (state ?: W96dState()).copy(available = false, connected = false, error = lastError)
                }
            }
            delay(2_000)
        }
    }

    DisposableEffect(outdoor) {
        onDispose {
            if (outdoor) scope.launch { runCatching { ble.disconnect() } }
        }
    }

    val owner = w96dOwner(environment, outdoor)
    val current = state

    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("W96D 控制来源", style = MaterialTheme.typography.titleMedium)
                Text("当前：${w96dOwnerLabel(owner)}", style = MaterialTheme.typography.titleLarge)
                Text(
                    when {
                        outdoor -> "OUTDOOR · 仅 BluetoothGatt，不访问 LAN"
                        environment == W96dEnvironment.HOME -> "HOME · ROUTER 24h"
                        owner == W96dOwner.NIGHT_NODE -> "SCHOOL · 23:00–06:00"
                        else -> "SCHOOL · 06:00–23:00"
                    },
                    style = MaterialTheme.typography.bodySmall,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(
                        enabled = !outdoor && !busy,
                        onClick = {
                            environment = W96dEnvironment.HOME
                            W96dPrefs.setEnvironment(context, environment)
                            state = null
                        },
                    ) { Text(if (environment == W96dEnvironment.HOME) "✓ HOME" else "HOME") }
                    OutlinedButton(
                        enabled = !outdoor && !busy,
                        onClick = {
                            environment = W96dEnvironment.SCHOOL
                            W96dPrefs.setEnvironment(context, environment)
                            state = null
                        },
                    ) { Text(if (environment == W96dEnvironment.SCHOOL) "✓ SCHOOL" else "SCHOOL") }
                }
                if (BuildConfig.NIGHT_NODE_ENABLED && !hasW96dBlePermissions(context)) {
                    OutlinedButton(onClick = { requestBlePermissions(false) }) { Text("授权 10S W96D 蓝牙") }
                }
                if (!outdoor) {
                    Button(enabled = !busy, onClick = ::requestOutdoor) { Text("进入外出模式 · 本机蓝牙") }
                } else {
                    Button(enabled = !busy, onClick = { scope.launch { exitOutdoor() } }) {
                        Text("退出外出模式 · 恢复 ${environment.name}")
                    }
                }
                Text(
                    "节点不会互相探活抢占。OUTDOOR 会先要求当前指定 owner 释放；退出时本机先断开，再恢复按环境/时间指定的 owner。",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }

        if (environment == W96dEnvironment.SCHOOL && !BuildConfig.NIGHT_NODE_ENABLED) {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("10S 夜间节点 API", style = MaterialTheme.typography.titleMedium)
                    OutlinedTextField(
                        value = nightUrl,
                        onValueChange = { nightUrl = it },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        label = { Text("例如 http://192.168.43.1:8766") },
                    )
                    OutlinedButton(onClick = {
                        runCatching { normalizeW96dBase(nightUrl) }
                            .onSuccess { W96dPrefs.setNightUrl(context, it); nightUrl = it; lastError = null }
                            .onFailure { lastError = it.message }
                    }) { Text("保存 10S 地址") }
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("设备状态", style = MaterialTheme.typography.titleMedium)
                Text(
                    when {
                        current?.connected == true -> "已连接${current.address?.let { " · $it" } ?: ""}"
                        busy -> "操作中…"
                        else -> "未连接"
                    }
                )
                current?.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
                lastError?.takeIf { it != current?.error }?.let {
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("控制", style = MaterialTheme.typography.titleMedium)
                ToggleRow("电源", current?.power, !busy && current?.available == true) { command("power", it) }
                Text("风速 $speedDraft%")
                Slider(
                    value = speedDraft.toFloat(),
                    onValueChange = { speedDraft = it.roundToInt().coerceIn(0, 100) },
                    onValueChangeFinished = { command("speed", speedDraft) },
                    valueRange = 0f..100f,
                    steps = 99,
                    enabled = !busy && current?.available == true,
                )
                ToggleRow("自然风", current?.natural, !busy && current?.available == true) { command("natural", it) }
                ToggleRow("Turbo", current?.turbo, !busy && current?.available == true) { command("turbo", it) }
                ToggleRow("指示灯", current?.indicator, !busy && current?.available == true) { command("indicator", it) }
                current?.turboRemainingSeconds?.takeIf { it > 0 }?.let {
                    Text("Turbo 剩余 ${it}s", style = MaterialTheme.typography.bodySmall)
                }
                Text("实体四档映射保持设备原设置；此滑杆只写 FFF3，不修改 FFF7。", style = MaterialTheme.typography.bodySmall)
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("遥测", style = MaterialTheme.typography.titleMedium)
                TelemetryRow("电池", voltageText(current?.batteryVoltageMv), currentText(current?.batteryCurrentMa))
                TelemetryRow("VBUS", voltageText(current?.vbusVoltageMv), currentText(current?.vbusCurrentMa))
                TelemetryRow("电机", voltageText(current?.motorVoltageMv), currentText(current?.motorCurrentMa))
                TelemetryRow("电池容量", current?.batteryCapacityMwh?.let { "$it mWh" } ?: "--", "")
                TelemetryRow("充放电", when (current?.chargeStatus) { 1 -> "充电中"; 0 -> "放电中"; else -> "--" }, "")
                TelemetryRow("电机状态", when (current?.motorBlocked) { true -> "阻转/异常"; false -> "正常"; null -> "--" }, "")
                current?.updatedAt?.let { Text("更新：$it", style = MaterialTheme.typography.labelSmall) }
            }
        }

        Spacer(Modifier.height(8.dp))
    }

    if (forceOutdoorDialog) {
        AlertDialog(
            onDismissRequest = { forceOutdoorDialog = false },
            title = { Text("指定节点未确认释放") },
            text = {
                Text(
                    "${w96dOwnerLabel(w96dOwner(environment, false))} 不可达：${releaseFailure ?: "未知错误"}\n\n" +
                        "为避免 BLE 竞争，只有在风扇已经离开原节点蓝牙范围时才应强制本机接管。"
                )
            },
            confirmButton = {
                Button(onClick = {
                    forceOutdoorDialog = false
                    scope.launch { enterOutdoor(true) }
                }) { Text("已离开范围，继续") }
            },
            dismissButton = { TextButton(onClick = { forceOutdoorDialog = false }) { Text("取消") } },
        )
    }
}

@Composable
private fun ToggleRow(label: String, value: Boolean?, enabled: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Column {
            Text(label)
            if (value == null) Text("未读取", style = MaterialTheme.typography.labelSmall)
        }
        Switch(checked = value == true, onCheckedChange = onChange, enabled = enabled)
    }
}

@Composable
private fun TelemetryRow(label: String, left: String, right: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, style = MaterialTheme.typography.bodyMedium)
        Text(listOf(left, right).filter(String::isNotBlank).joinToString(" · "), style = MaterialTheme.typography.bodyMedium)
    }
    HorizontalDivider()
}

private fun voltageText(value: Number?): String = value?.toDouble()?.let { "%.2f V".format(it / 1000.0) } ?: "--"
private fun currentText(value: Number?): String = value?.toDouble()?.let {
    if (kotlin.math.abs(it) >= 1000) "%.2f A".format(it / 1000.0) else "${it.toInt()} mA"
} ?: "--"
