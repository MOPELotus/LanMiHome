package dev.lotus.lanmihome

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

@Composable
fun LightStripScreen(state: LightStripState?, enabled: Boolean, patch: (Array<Pair<String, Any>>) -> Unit) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        if (state == null) {
            CircularProgressIndicator()
            Text("正在读取灯带状态…")
            return@Column
        }
        val ready = state.available && state.controlReady && enabled
        val statusText = when (state.status) {
            "ready" -> "已连接"
            "not_configured" -> "待配置设备"
            "disabled" -> "已停用"
            "protocol_pending" -> "协议待实物验证"
            "backend_unsupported" -> "路由器尚未安装灯带模块"
            "backend_unavailable" -> "暂时无法连接路由器"
            else -> "设备暂时不可用"
        }
        Section("灯带") {
            Text("${state.model} · 单色调光")
            state.firmware?.let { Text("固件 $it", style = MaterialTheme.typography.bodySmall) }
            Text(statusText)
            state.address?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        }
        if (!state.available || !state.controlReady) {
            Section("未连接说明") {
                Text("设备未接入前，开关与亮度控制保持禁用，不会发送蓝牙指令。")
                Text("路由器需配置 SP651E 的蓝牙地址和直接控制协议；当前已验证固件 V3020.07。无需米家或 BanlanX 绑定。")
                Text("仅提供开关和亮度入口，不包含 RGB、色温或流水效果。")
            }
        }
        Section("电源") {
            val powerLabel = when (state.power) {
                true -> "已开启"
                false -> "已关闭"
                null -> "状态未知"
            }
            ToggleLine(powerLabel, state.power == true, ready) { patch(arrayOf("power" to it)) }
        }
        var brightness by remember(state.brightness) {
            mutableFloatStateOf((state.brightness ?: 50).coerceIn(1, 100).toFloat())
        }
        Section(if (state.brightness == null) "亮度 · 待接入" else "亮度 · ${brightness.roundToInt()}%") {
            Slider(
                value = brightness,
                onValueChange = { brightness = it },
                valueRange = 1f..100f,
                enabled = ready,
                onValueChangeFinished = { patch(arrayOf("brightness" to brightness.roundToInt())) },
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf(25, 50, 75, 100).forEach { value ->
                    OutlinedButton(
                        onClick = { patch(arrayOf("brightness" to value)) },
                        enabled = ready,
                        modifier = Modifier.weight(1f),
                        contentPadding = PaddingValues(horizontal = 0.dp),
                    ) {
                        Text("$value%", maxLines = 1)
                    }
                }
            }
            Text("调节亮度会切换到常亮模式，并保持当前开关状态。", style = MaterialTheme.typography.bodySmall)
            if (state.mode != null && state.mode != 2) {
                Text("当前不是常亮模式；调光后将退出动态或音乐效果。", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}
