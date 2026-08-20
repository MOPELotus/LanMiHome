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
                Text("路由器 BLE 状态", style = MaterialTheme.typography.titleMedium)
                Text(if (server?.available == true) "RAX3000M：测量数据新鲜" else "RAX3000M：暂无新测量")
                server?.ageSeconds?.let { Text("最后有效测量：${it} 秒前") }
                server?.rssi?.let { Text("BlueZ 接收 RSSI：$it dBm") }
                server?.mac?.let { Text("温湿度计：$it") }
                server?.receivedAt?.let { Text("服务端时间：$it") }
                if ((server?.reports ?: 0) > 0) Text("有效测量帧：${server?.reports}")
            }
        }

        Text(
            "温湿度计由 RAX3000M 的 BlueZ 持续扫描并在路由器本地解密；本客户端只读取 LAN API，不申请蓝牙权限、不保存 BLE Key。",
            style = MaterialTheme.typography.bodySmall,
        )
    }
}
