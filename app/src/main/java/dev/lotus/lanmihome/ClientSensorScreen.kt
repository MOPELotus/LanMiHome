package dev.lotus.lanmihome

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun ClientSensorScreen(server: SensorState?) {
    fun value(v: Double?, suffix: String) = v?.let { "%.1f%s".format(it, suffix) } ?: "—"

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("房间环境", style = MaterialTheme.typography.headlineSmall)

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("当前环境", style = MaterialTheme.typography.titleMedium)
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Column {
                        Text("温度", style = MaterialTheme.typography.labelMedium)
                        Text(value(server?.temperature, "°C"), style = MaterialTheme.typography.headlineMedium)
                    }
                    Column {
                        Text("湿度", style = MaterialTheme.typography.labelMedium)
                        Text(value(server?.humidity, "%"), style = MaterialTheme.typography.headlineMedium)
                    }
                    Column {
                        Text("电量", style = MaterialTheme.typography.labelMedium)
                        Text(server?.battery?.let { "$it%" } ?: "—", style = MaterialTheme.typography.headlineMedium)
                    }
                }
            }
        }

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(7.dp)) {
                Text("设备状态", style = MaterialTheme.typography.titleMedium)
                Text(if (server?.available == true) "数据同步正常" else "等待新的测量数据")
                server?.ageSeconds?.let { Text("上次更新：${it} 秒前") }
                server?.rssi?.let { Text("信号强度：$it dBm") }
            }
        }

        Text(
            "环境数据会由室内设备自动同步，手机无需保持蓝牙连接。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
