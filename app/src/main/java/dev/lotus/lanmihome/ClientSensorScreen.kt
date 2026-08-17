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
        Text("温湿度计", style = MaterialTheme.typography.headlineSmall)

        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("宿舍实时数据", style = MaterialTheme.typography.titleMedium)
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
                Text("数据状态", style = MaterialTheme.typography.titleMedium)
                Text(if (server?.available == true) "RAX3000M：已有网关上报数据" else "RAX3000M：暂无新数据")
                server?.ageSeconds?.let { Text("最后收到：${it} 秒前") }
                server?.rssi?.let { Text("网关接收 RSSI：$it dBm") }
                server?.mac?.let { Text("温湿度计：$it") }
                server?.receivedAt?.let { Text("服务端时间：$it") }
                if ((server?.reports ?: 0) > 0) Text("累计上报：${server?.reports}")
            }
        }

        Text(
            "本客户端只从 RAX3000M 读取温湿度计数据，不扫描蓝牙、不保存 BLE Key，也不进行本机解密。",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}
