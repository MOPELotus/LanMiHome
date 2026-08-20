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
        Text("酷态科充电头", style = MaterialTheme.typography.headlineSmall)

        if (chargers == null) {
            CircularProgressIndicator()
            Text("正在读取充电头…")
            return@Column
        }
        if (chargers.isEmpty()) {
            Text("服务端未返回已配置的充电头。")
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

        Text(
            "实时状态来自路由器上的 AD1204 BLE 长连接。页面切换（PIID 14 / goto）尚未确认真实设备语义，因此客户端暂不提供该操作。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
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
    val label = charger.name.replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }
    val detail = listOfNotNull(
        charger.address,
        charger.firmwareVersion?.let { "FW $it" },
        charger.miotVersion?.let { "MIoT $it" },
    ).joinToString(" · ").takeIf { it.isNotBlank() }

    DeviceHeader(
        title = label,
        detail = detail,
        available = ok,
        error = if (ok) null else charger.status ?: "未连接或未认证",
    )

    Section("实时功率 · ${"%.2f".format(charger.totalPower)} W") {
        charger.deviceModel?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        charger.updatedAt?.let { Text("状态时间：$it", style = MaterialTheme.typography.bodySmall) }
        if (charger.c3aShared) {
            Text("C3 + A 当前处于共享输出状态", color = MaterialTheme.colorScheme.primary)
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

    Section("场景模式") {
        val mode = charger.setting(5)?.toInt()
        Row(
            Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            listOf(1 to "AI", 2 to "数码", 3 to "单口", 4 to "均衡").forEach { (value, text) ->
                FilterChip(
                    selected = mode == value,
                    onClick = { patch(charger.name, arrayOf("scene_mode" to value)) },
                    label = { Text(text) },
                    enabled = ok && enabled,
                )
            }
        }
    }

    Section("屏幕") {
        val timeout = charger.setting(6)?.toInt()
        Text("熄屏时间", style = MaterialTheme.typography.labelLarge)
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
        Text("语言", style = MaterialTheme.typography.labelLarge)
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

    Section("USB-A") {
        ToggleLine(
            label = "低电流模式",
            checked = charger.setting(15) == 1L,
            enabled = ok && enabled,
            note = "用于小电流设备；不会修改 C 口设置",
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
            Text("共享输出：C3 + A", color = MaterialTheme.colorScheme.primary)
        }

        port.protocolSource?.let {
            Text("协议来源：$it${port.protocolNumber?.let { n -> " · #$n" } ?: ""}", style = MaterialTheme.typography.bodySmall)
        }

        ToggleLine(
            label = "允许端口输出",
            checked = port.enabled,
            enabled = enabled,
            note = if (port.active) "当前端口有负载；关闭会立即断电" else null,
        ) { setPort(chargerName, port.key, it) }

        if (protocols.isNotEmpty()) {
            HorizontalDivider()
            Text("协议开关", style = MaterialTheme.typography.labelLarge)
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
            if (timerMinutes > 0) "倒计时：$timerMinutes 分钟" else "倒计时：关闭",
            style = MaterialTheme.typography.labelLarge,
        )
        Row(
            Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            listOf(0, 30, 60, 120).forEach { minutes ->
                AssistChip(
                    onClick = { setTimer(chargerName, port.key, minutes) },
                    label = { Text(if (minutes == 0) "关闭" else "$minutes 分") },
                    enabled = enabled,
                )
            }
        }
    }
}
