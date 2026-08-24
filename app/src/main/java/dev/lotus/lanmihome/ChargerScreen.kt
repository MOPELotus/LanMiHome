package dev.lotus.lanmihome

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun ChargerScreen(
    chargers: List<ChargerState>?,
    enabled: Boolean,
    patch: (String, Array<Pair<String, Any>>) -> Unit,
    setPort: (String, String, Boolean) -> Unit,
    setProtocol: (String, String, String, Boolean) -> Unit,
    setTimer: (String, String, Int) -> Unit,
) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("充电设备", style = MaterialTheme.typography.headlineSmall)

        if (chargers == null) {
            CircularProgressIndicator()
            Text("正在连接充电设备…")
            return@Column
        }
        if (chargers.isEmpty()) {
            Text("暂时没有可用的充电设备。")
            return@Column
        }

        chargers.forEachIndexed { index, charger ->
            ChargerDevice(
                charger = charger,
                enabled = enabled,
                patch = patch,
                setPort = setPort,
                setProtocol = setProtocol,
                setTimer = setTimer,
            )
            if (index != chargers.lastIndex) {
                HorizontalDivider(Modifier.padding(vertical = 6.dp))
            }
        }
    }
}

private fun chargerDisplayName(name: String): String = when (name.lowercase()) {
    "bed" -> "床头充电头"
    "desk" -> "桌面充电头"
    else -> name.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }
}

@Composable
private fun ChargerDevice(
    charger: ChargerState,
    enabled: Boolean,
    patch: (String, Array<Pair<String, Any>>) -> Unit,
    setPort: (String, String, Boolean) -> Unit,
    setProtocol: (String, String, String, Boolean) -> Unit,
    setTimer: (String, String, Int) -> Unit,
) {
    val ok = charger.available
    val detail = charger.firmwareVersion?.let { "固件 $it" }

    DeviceHeader(
        title = chargerDisplayName(charger.name),
        detail = detail,
        available = ok,
        error = if (ok) null else charger.status ?: "设备暂时离线",
    )

    Section("总功率 · ${"%.2f".format(charger.totalPower)} W") {
        if (charger.c3aShared) {
            Text("C3 与 USB-A 正在共享输出功率", color = MaterialTheme.colorScheme.primary)
        } else {
            Text("各端口会根据当前负载自动分配功率", style = MaterialTheme.typography.bodySmall)
        }
    }

    charger.ports.values.forEach { port ->
        ChargerPort(
            chargerName = charger.name,
            port = port,
            protocols = charger.protocolSwitches[port.key].orEmpty(),
            enabled = ok && enabled,
            timerMinutes = charger.setting(
                when (port.key) {
                    "c1" -> 9
                    "c2" -> 10
                    "c3" -> 11
                    else -> 12
                }
            )?.toInt() ?: 0,
            setPort = setPort,
            setProtocol = setProtocol,
            setTimer = setTimer,
        )
    }

    Section("使用模式") {
        val mode = charger.setting(5)?.toInt()
        Row(
            Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            listOf(1 to "智能", 2 to "数码设备", 3 to "单口优先", 4 to "均衡").forEach { (value, text) ->
                FilterChip(
                    selected = mode == value,
                    onClick = { patch(charger.name, arrayOf("scene_mode" to value)) },
                    label = { Text(text) },
                    enabled = ok && enabled,
                )
            }
        }
    }

    Section("屏幕设置") {
        val timeout = charger.setting(6)?.toInt()
        Text("自动熄屏", style = MaterialTheme.typography.labelLarge)
        Row(
            Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            listOf(
                5 to "1 分钟",
                1 to "5 分钟",
                2 to "10 分钟",
                3 to "30 分钟",
                4 to "常亮",
            ).forEach { (value, text) ->
                FilterChip(
                    selected = timeout == value,
                    onClick = { patch(charger.name, arrayOf("screen_timeout" to value)) },
                    label = { Text(text) },
                    enabled = ok && enabled,
                )
            }
        }

        HorizontalDivider()

        val language = charger.setting(13)?.toInt()
        Text("显示语言", style = MaterialTheme.typography.labelLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = language == 1,
                onClick = { patch(charger.name, arrayOf("language" to 1)) },
                label = { Text("中文") },
                enabled = ok && enabled,
            )
            FilterChip(
                selected = language == 0,
                onClick = { patch(charger.name, arrayOf("language" to 0)) },
                label = { Text("English") },
                enabled = ok && enabled,
            )
        }

        HorizontalDivider()

        ToggleLine(
            label = "空闲时关闭屏幕",
            checked = charger.setting(19) == 1L,
            enabled = ok && enabled,
        ) { patch(charger.name, arrayOf("idle_screen_off" to it)) }

        HorizontalDivider()

        ToggleLine(
            label = "锁定屏幕方向",
            checked = charger.setting(20) == 1L,
            enabled = ok && enabled,
        ) { patch(charger.name, arrayOf("orientation_lock" to it)) }
    }

    Section("USB-A 设置") {
        ToggleLine(
            label = "小电流模式",
            checked = charger.setting(15) == 1L,
            enabled = ok && enabled,
            note = "适合耳机、手环等低功耗设备",
        ) { patch(charger.name, arrayOf("usb_a_low_current" to it)) }
    }
}

@Composable
private fun ChargerPort(
    chargerName: String,
    port: ChargerPortState,
    protocols: Map<String, Boolean>,
    enabled: Boolean,
    timerMinutes: Int,
    setPort: (String, String, Boolean) -> Unit,
    setProtocol: (String, String, String, Boolean) -> Unit,
    setTimer: (String, String, Int) -> Unit,
) {
    Section("${port.name} · ${"%.2f".format(port.power)} W") {
        Text(
            "${"%.1f".format(port.voltage)} V · ${"%.1f".format(port.current)} A · " +
                if (port.active) port.protocol.uppercase() else "空闲",
            style = MaterialTheme.typography.titleMedium,
        )

        if (port.shared) {
            Text("与另一端口共享输出功率", color = MaterialTheme.colorScheme.primary)
        }

        ToggleLine(
            label = "端口供电",
            checked = port.enabled,
            enabled = enabled,
            note = if (port.active) "关闭后，此端口会立即停止供电" else null,
        ) { setPort(chargerName, port.key, it) }

        if (protocols.isNotEmpty()) {
            HorizontalDivider()
            Text("快充协议", style = MaterialTheme.typography.labelLarge)
            protocols.toSortedMap().forEach { (protocol, checked) ->
                ToggleLine(
                    label = protocol.uppercase(),
                    checked = checked,
                    enabled = enabled,
                ) { setProtocol(chargerName, port.key, protocol, it) }
            }
        }

        HorizontalDivider()
        Text(
            if (timerMinutes > 0) "定时关闭：$timerMinutes 分钟" else "定时关闭：未开启",
            style = MaterialTheme.typography.labelLarge,
        )
        Row(
            Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            listOf(0, 30, 60, 120).forEach { minutes ->
                AssistChip(
                    onClick = { setTimer(chargerName, port.key, minutes) },
                    label = { Text(if (minutes == 0) "关闭定时" else "$minutes 分钟") },
                    enabled = enabled,
                )
            }
        }
    }
}
