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
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
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
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

@Composable
internal fun W96dScreen(primaryBase: String) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val ble = remember { W96dGattClient(context.applicationContext) }
    var environment by remember { mutableStateOf(W96dPrefs.environment(context)) }
    var outdoor by remember { mutableStateOf(W96dPrefs.outdoor(context)) }
    var state by remember { mutableStateOf<W96dState?>(null) }
    var busy by remember { mutableStateOf(false) }
    var lastError by remember { mutableStateOf<String?>(null) }
    var speedDraft by remember { mutableIntStateOf(50) }
    var forceOutdoorDialog by remember { mutableStateOf(false) }
    var pendingPermissionOutdoor by remember { mutableStateOf(false) }

    fun remote(owner: W96dOwner): W96dRemoteClient = when (owner) {
        W96dOwner.ROUTER -> W96dRemoteClient(routerW96dBase(primaryBase))
        W96dOwner.NIGHT_NODE -> error("该连接方式当前不可用")
        W96dOwner.PHONE -> error("手机直连无需网络连接")
    }

    suspend fun enterOutdoor(forceWithoutRelease: Boolean = false) {
        val previousOwner = w96dOwner(environment, false)
        var released = false
        busy = true
        try {
            if (!forceWithoutRelease) {
                try {
                    remote(previousOwner).release()
                    released = true
                } catch (_: Exception) {
                    forceOutdoorDialog = true
                    return
                }
            }

            W96dPrefs.setOutdoor(context, true)
            outdoor = true
            try {
                state = ble.connect()
                speedDraft = state?.speed ?: speedDraft
                lastError = null
            } catch (_: Exception) {
                W96dPrefs.setOutdoor(context, false)
                outdoor = false
                state = W96dState(owner = "phone")
                lastError = "无法使用手机直连，请确认 W96D 已开启并靠近手机"
                if (released) runCatching { remote(previousOwner).resume() }
            }
        } finally {
            busy = false
        }
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
            } catch (_: Exception) {
                lastError = "已结束外出模式，路由器正在重新连接 W96D"
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
            result[it] == true ||
                context.checkSelfPermission(it) == android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        if (granted && pendingPermissionOutdoor) {
            pendingPermissionOutdoor = false
            scope.launch { enterOutdoor(false) }
        } else if (!granted) {
            pendingPermissionOutdoor = false
            lastError = "允许附近设备权限后即可使用外出模式"
        }
    }

    fun requestBlePermissions() {
        pendingPermissionOutdoor = true
        permissionLauncher.launch(requiredW96dBlePermissions())
    }

    fun requestOutdoor() {
        if (!hasW96dBlePermissions(context)) requestBlePermissions()
        else scope.launch { enterOutdoor(false) }
    }

    fun command(key: String, value: Any) {
        scope.launch {
            busy = true
            try {
                state = if (outdoor) {
                    if (!hasW96dBlePermissions(context)) throw IllegalStateException("missing permission")
                    ble.patch(key, value)
                } else {
                    remote(w96dOwner(environment, false)).patch(key to value)
                }
                speedDraft = state?.speed ?: speedDraft
                lastError = null
            } catch (_: Exception) {
                lastError = "操作未完成，请稍后重试"
            } finally {
                busy = false
            }
        }
    }

    LaunchedEffect(environment, outdoor, primaryBase) {
        while (isActive) {
            if (!busy) {
                try {
                    val next = if (outdoor) {
                        if (!hasW96dBlePermissions(context)) {
                            W96dState(owner = "phone")
                        } else {
                            ble.connect()
                        }
                    } else {
                        remote(w96dOwner(environment, false)).state()
                    }
                    state = next
                    next.speed?.let { speedDraft = it }
                    if (next.error == null) lastError = null
                } catch (_: Exception) {
                    state = (state ?: W96dState()).copy(
                        available = false,
                        connected = false,
                    )
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

    val current = state
    val sceneLabel = when {
        outdoor -> "外出"
        environment == W96dEnvironment.HOME -> "家里"
        else -> "宿舍"
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("使用场景", style = MaterialTheme.typography.titleMedium)
                Text(sceneLabel, style = MaterialTheme.typography.titleLarge)
                Text(
                    if (outdoor) {
                        "手机会直接连接 W96D，适合离开家或宿舍后使用。"
                    } else {
                        "W96D 会通过当前路由器保持连接，打开 App 即可控制。"
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
                    ) { Text(if (environment == W96dEnvironment.HOME) "✓ 家里" else "家里") }
                    OutlinedButton(
                        enabled = !outdoor && !busy,
                        onClick = {
                            environment = W96dEnvironment.SCHOOL
                            W96dPrefs.setEnvironment(context, environment)
                            state = null
                        },
                    ) { Text(if (environment == W96dEnvironment.SCHOOL) "✓ 宿舍" else "宿舍") }
                }
                if (!outdoor) {
                    Button(enabled = !busy, onClick = ::requestOutdoor) {
                        Text("切换到外出模式")
                    }
                } else {
                    Button(enabled = !busy, onClick = { scope.launch { exitOutdoor() } }) {
                        Text("结束外出模式")
                    }
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("连接状态", style = MaterialTheme.typography.titleMedium)
                Text(
                    when {
                        current?.connected == true -> "W96D 已连接"
                        busy -> "正在连接 W96D…"
                        else -> "W96D 暂时离线"
                    },
                    style = MaterialTheme.typography.titleMedium,
                )
                if (current?.error != null && current.connected != true) {
                    Text("正在自动重试，无需手动操作", style = MaterialTheme.typography.bodySmall)
                }
                lastError?.let {
                    Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("风扇控制", style = MaterialTheme.typography.titleMedium)
                ToggleRow("电源", current?.power, !busy && current?.available == true) {
                    command("power", it)
                }
                Text("风速 $speedDraft%")
                Slider(
                    value = speedDraft.toFloat(),
                    onValueChange = { speedDraft = it.roundToInt().coerceIn(0, 100) },
                    onValueChangeFinished = { command("speed", speedDraft) },
                    valueRange = 0f..100f,
                    steps = 99,
                    enabled = !busy && current?.available == true,
                )
                ToggleRow("自然风", current?.natural, !busy && current?.available == true) {
                    command("natural", it)
                }
                ToggleRow("Turbo 强劲模式", current?.turbo, !busy && current?.available == true) {
                    command("turbo", it)
                }
                ToggleRow("指示灯", current?.indicator, !busy && current?.available == true) {
                    command("indicator", it)
                }
                current?.turboRemainingSeconds?.takeIf { it > 0 }?.let {
                    Text("强劲模式剩余 ${it} 秒", style = MaterialTheme.typography.bodySmall)
                }
                Text("风速支持 0–100% 无级调节。", style = MaterialTheme.typography.bodySmall)
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("设备状态", style = MaterialTheme.typography.titleMedium)
                TelemetryRow("电池", voltageText(current?.batteryVoltageMv), currentText(current?.batteryCurrentMa))
                TelemetryRow("外部供电", voltageText(current?.vbusVoltageMv), "")
                TelemetryRow("电机", voltageText(current?.motorVoltageMv), currentText(current?.motorCurrentMa))
                TelemetryRow("电池容量", capacityText(current?.batteryCapacityMwh), "")
                current?.updatedAt?.let {
                    Text("最近更新 ${displayTime(it)}", style = MaterialTheme.typography.labelSmall)
                }
            }
        }

        Spacer(Modifier.height(8.dp))
    }

    if (forceOutdoorDialog) {
        AlertDialog(
            onDismissRequest = { forceOutdoorDialog = false },
            title = { Text("无法切换到外出模式") },
            text = {
                Text(
                    "暂时无法通知室内路由器释放 W96D。若你已经离开家或宿舍，可以继续尝试手机直连；仍在室内时建议取消。"
                )
            },
            confirmButton = {
                Button(
                    onClick = {
                        forceOutdoorDialog = false
                        scope.launch { enterOutdoor(true) }
                    },
                ) { Text("继续手机直连") }
            },
            dismissButton = {
                TextButton(onClick = { forceOutdoorDialog = false }) { Text("取消") }
            },
        )
    }
}

@Composable
private fun ToggleRow(
    label: String,
    value: Boolean?,
    enabled: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Column {
            Text(label)
            if (value == null) Text("等待同步", style = MaterialTheme.typography.labelSmall)
        }
        Switch(checked = value == true, onCheckedChange = onChange, enabled = enabled)
    }
}

@Composable
private fun TelemetryRow(label: String, left: String, right: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label, style = MaterialTheme.typography.bodyMedium)
        Text(
            listOf(left, right).filter(String::isNotBlank).joinToString(" · "),
            style = MaterialTheme.typography.bodyMedium,
        )
    }
    HorizontalDivider()
}

private fun voltageText(value: Number?): String =
    value?.toDouble()?.let { "%.2f V".format(it / 1000.0) } ?: "—"

private fun currentText(value: Number?): String = value?.toDouble()?.let {
    if (kotlin.math.abs(it) >= 1000) "%.2f A".format(it / 1000.0) else "${it.toInt()} mA"
} ?: "—"

private fun capacityText(value: Long?): String =
    value?.let { "%.1f Wh".format(it / 1000.0) } ?: "—"

private fun displayTime(value: String): String =
    value.substringAfter('T', value).take(8).ifBlank { value }
