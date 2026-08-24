package dev.lotus.lanmihome

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

@Composable
fun FanScreen(state:FanState?, recovery:RecoveryState?, enabled:Boolean, patch:(Array<Pair<String,Any>>)->Unit, action:(String)->Unit, recover:()->Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement=Arrangement.spacedBy(12.dp)) {
        if(state==null) { CircularProgressIndicator(); Text("正在连接风扇…"); return@Column }
        val ok=state.available
        DeviceHeader(state.name ?: "米家直流变频落地扇 1X", state.ip, ok, state.error)

        Section("电源") {
            ToggleLine(if(state.power==true) "已开启" else "已关闭", state.power==true, ok&&enabled) {
                patch(arrayOf("power" to it))
            }
        }

        var speed by remember(state.speed) { mutableFloatStateOf((state.speed ?: 1).toFloat()) }
        Section("风速 · ${speed.roundToInt()}%") {
            Slider(speed, {speed=it}, valueRange=1f..100f, enabled=ok&&enabled,
                onValueChangeFinished={patch(arrayOf("speed" to speed.roundToInt()))})
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(1 to "轻柔",35 to "舒适",70 to "强劲",100 to "最大").forEach { (v,n) ->
                    AssistChip(onClick={patch(arrayOf("speed" to v))}, enabled=ok&&enabled, label={Text(n)})
                }
            }
        }

        Section("风感") {
            Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                FilterChip(state.mode==0,{patch(arrayOf("mode" to "straight"))},{Text("直吹")},enabled=ok&&enabled)
                FilterChip(state.mode==1,{patch(arrayOf("mode" to "natural"))},{Text("自然风")},enabled=ok&&enabled)
            }
        }

        Section("摆风") {
            ToggleLine("左右摆风", state.swing==true, ok&&enabled) { patch(arrayOf("swing" to it)) }
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(30,60,90,120,140).forEach { a ->
                    FilterChip(state.swingAngle==a,{patch(arrayOf("swing_angle" to a))},{Text("$a°")},enabled=ok&&enabled)
                }
            }
            Row(horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                OutlinedButton({action("turn-left")},Modifier.weight(1f),enabled=ok&&enabled){Text("向左微调")}
                OutlinedButton({action("turn-right")},Modifier.weight(1f),enabled=ok&&enabled){Text("向右微调")}
            }
        }

        var timer by remember(state.offDelay) { mutableFloatStateOf((state.offDelay ?: 0).toFloat()) }
        Section("定时关闭 · ${timer.roundToInt()} 分钟") {
            Slider(timer,{timer=it},valueRange=0f..480f,enabled=ok&&enabled,
                onValueChangeFinished={patch(arrayOf("off_delay_minutes" to timer.roundToInt()))})
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                listOf(0,30,60,120,240,480).forEach { m ->
                    AssistChip({patch(arrayOf("off_delay_minutes" to m))},label={Text(if(m==0)"关闭定时" else "${m} 分钟")},enabled=ok&&enabled)
                }
            }
        }

        Section("设备设置") {
            ToggleLine("指示灯",state.indicator==true,ok&&enabled){patch(arrayOf("indicator" to it))}
            HorizontalDivider()
            ToggleLine("按键提示音",state.alarm==true,ok&&enabled){patch(arrayOf("alarm" to it))}
            HorizontalDivider()
            ToggleLine("童锁",state.childLock==true,ok&&enabled,"开启后将锁定机身按键"){patch(arrayOf("child_lock" to it))}
        }

        Section("断电恢复") {
            Text(
                when {
                    recovery==null -> "自动恢复已开启"
                    recovery.active -> "正在恢复风扇…"
                    recovery.success -> "风扇已恢复"
                    else -> "等待下次供电恢复"
                }
            )
            if (recovery?.error != null) {
                Text("上次恢复未完成，将在下次自动重试", color=MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Text("宿舍恢复供电后，会自动尝试将风扇恢复到可用状态。",style=MaterialTheme.typography.bodySmall)
            OutlinedButton(recover,enabled=enabled){Text("立即恢复")}
        }
    }
}
